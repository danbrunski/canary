"""``digest.json`` end-of-drive summary sidecar (Task 5.5,
eval-longitudinal-and-dtc.md).

Assembled once per trip end (Task 5.4's sustained-GPS-stop edge, or the
daemon-shutdown fallback), not recomputed on every request like the other
post-hoc sidecars — a trip only ends once, and re-running it after the fact
would just reproduce the same aggregation over the same closed session.
Nothing here is new physics: every category is a small aggregation over
sidecars/logs the earlier phases of this doc already compute independently:

* duration / mode seconds  <- :func:`~src.analysis.session_classifier.classify_session`
* fingerprint stats + top movers vs the frozen reference epoch <-
  :func:`~src.analysis.fingerprint.compute_fingerprint` /
  :mod:`~src.analysis.reference_epoch`
* gated anomaly counts by source <- the ``anomalies-*.jsonl`` log
  (:mod:`~src.alerts.event_logger`), windowed to this session
* new DTCs <- the session's own ``dtc.json`` (Task 1.7) — ``DtcState``
  already re-flags a code as "new" for every session it's seen in (its own
  per-session ``_seen`` baseline resets on a session change), so everything
  in that sidecar for THIS session is "new" by that module's own definition
* new ledger events <- :class:`~src.analysis.vehicle_ledger.VehicleLedgerStore`
  entries recorded during the session's time window
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import timedelta as _timedelta
from pathlib import Path
from typing import Any

from src.alerts.event_logger import event_log_path, read_events
from src.analysis.fingerprint import compute_fingerprint
from src.analysis.reference_epoch import read_reference_epoch
from src.analysis.session_classifier import classify_session
from src.analysis.vehicle_ledger import VehicleLedgerStore
from src.atomic_write import atomic_write_text
from src.config import CanaryConfig
from src.control.dtc_state import SIDECAR_FILENAME as DTC_SIDECAR_FILENAME
from src.timefmt import format_timestamp as _format_timestamp
from src.timefmt import parse_iso_utc

log = logging.getLogger(__name__)

DIGEST_FILENAME = "digest.json"
SCHEMA_VERSION = 1

#: How many entries to keep in the compact "top movers" / "top deviations"
#: lists — a digest is meant to be skimmed (and pushed through a
#: notification), not a full report; that's what fingerprint.json /
#: trip_summary.json already are.
_TOP_N = 5

#: Prefix fingerprint stats append for behavior-surface deviations
#: (mirrors ``src.decode.behavior_decoder.DEVIATION_COLUMN_PREFIX`` stripped
#: of its ``deviation:`` wide-column form — see
#: ``fingerprint._add_behavior_deviation_stats``).
_BEHAVIOR_DEVIATION_STAT_PREFIX = "behavior_deviation_abs_mean:"

_SESSION_DIR_FORMAT = "%Y-%m-%dT%H%M%S"
_ONE_DAY = _timedelta(days=1)


@dataclass(frozen=True)
class SessionDigest:
    """Full digest sidecar payload for one completed trip."""

    schema_version: int
    computed_at: str
    session_id: str
    duration_seconds: float
    mode_seconds: dict[str, float]
    anomaly_counts_by_source: dict[str, int]
    top_behavior_deviations: list[dict[str, Any]]
    fingerprint_top_movers: list[dict[str, Any]]
    new_dtcs: list[dict[str, Any]]
    new_ledger_events: list[dict[str, Any]]
    #: Tuning section, present only for capture sessions. ``None`` on an
    #: ordinary drive rather than an empty block, so a reader can tell "not a
    #: tuning capture" from "a capture that found nothing".
    tuning: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "computed_at": self.computed_at,
            "session_id": self.session_id,
            "duration_seconds": self.duration_seconds,
            "mode_seconds": dict(self.mode_seconds),
            "anomaly_counts_by_source": dict(self.anomaly_counts_by_source),
            "top_behavior_deviations": list(self.top_behavior_deviations),
            "fingerprint_top_movers": list(self.fingerprint_top_movers),
            "new_dtcs": list(self.new_dtcs),
            "new_ledger_events": list(self.new_ledger_events),
            "tuning": self.tuning,
        }


def _session_start(session_dir: Path) -> datetime | None:
    from src.control.sessions import session_started_at

    return session_started_at(session_dir)


def _anomaly_counts_by_source(
    anomalies_dir: Path, start: datetime, end: datetime
) -> dict[str, int]:
    """Count gated anomaly events whose timestamp falls in ``[start, end]``.

    Only reads the specific per-day files the window can possibly span
    (daily rotation, see ``event_log_path``) rather than every file the
    anomalies directory has ever accumulated.
    """
    counts: dict[str, int] = {}
    day = start.date()
    last_day = end.date()
    while day <= last_day:
        path = event_log_path(anomalies_dir, day.isoformat())
        if path.exists():
            for event in read_events(path):
                try:
                    ts = parse_iso_utc(event.timestamp)
                except ValueError:
                    continue
                if start <= ts <= end:
                    counts[event.source] = counts.get(event.source, 0) + 1
        day += _ONE_DAY
    return counts


def _read_dtc_sidecar(session_dir: Path) -> dict[str, Any] | None:
    path = session_dir / DTC_SIDECAR_FILENAME
    if not path.exists():
        return None
    try:
        return dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        return None


def _new_dtcs(session_dir: Path) -> list[dict[str, Any]]:
    """Every stored/pending/permanent code in this session's ``dtc.json``.

    ``DtcState`` resets its "already seen" baseline on every session change
    (see its module docstring), so a code the ECU still carries over from a
    prior drive is deliberately re-flagged as new for THIS session's
    sidecar — the digest inherits that same definition rather than
    re-deriving its own.
    """
    report = _read_dtc_sidecar(session_dir)
    if report is None:
        return []
    codes: dict[str, dict[str, Any]] = {}
    for status in ("stored", "pending", "permanent"):
        for entry in report.get(status) or []:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code", "?"))
            merged = dict(entry)
            merged["status"] = status
            codes.setdefault(code, merged)
    return list(codes.values())


def _new_ledger_events(
    ledger: VehicleLedgerStore, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Ledger events recorded (append timestamp, not ``occurred_at``) during
    this session's time window — the auto-appended DTC entries (Task 3.4)
    and any operator/MCP write made during the drive."""
    events: list[dict[str, Any]] = []
    for record in ledger.records():
        try:
            recorded = datetime.fromisoformat(record.recorded_at)
        except ValueError:
            continue
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=UTC)
        if start <= recorded <= end:
            events.append(record.to_dict())
    return events


def _top_behavior_deviations(stats: dict[str, float]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = [
        {"channel": name[len(_BEHAVIOR_DEVIATION_STAT_PREFIX) :], "abs_mean_deviation": value}
        for name, value in stats.items()
        if name.startswith(_BEHAVIOR_DEVIATION_STAT_PREFIX)
    ]
    entries.sort(key=lambda e: abs(float(e["abs_mean_deviation"])), reverse=True)
    return entries[:_TOP_N]


def _fingerprint_top_movers(
    stats: dict[str, float], reference_stats: dict[str, Any]
) -> list[dict[str, Any]]:
    """Rank this session's fingerprint stats by |z-score| against the frozen
    reference epoch. Stats the reference epoch never saw are skipped —
    nothing to compare against."""
    movers: list[dict[str, Any]] = []
    for name, value in stats.items():
        summary = reference_stats.get(name)
        if summary is None:
            continue
        std = float(summary.std)
        mean = float(summary.mean)
        z = (value - mean) / std if std > 0 else None
        movers.append(
            {
                "stat": name,
                "value": value,
                "reference_mean": mean,
                "reference_std": std,
                "z_score": z,
            }
        )
    movers.sort(key=lambda m: abs(m["z_score"]) if m["z_score"] is not None else -1, reverse=True)
    return movers[:_TOP_N]


def _tuning_section(session_dir: Path, config: CanaryConfig) -> dict[str, Any] | None:
    """Tuning digest for a capture session; ``None`` for an ordinary drive.

    Never raises: a normal drive's digest must not fail because the tuning
    section had nothing to say, or because a vehicle has no AFR calibration.
    """
    from src.analysis.tuning_digest import compute_tuning_digest

    try:
        section = compute_tuning_digest(session_dir, config)
    except Exception:  # noqa: BLE001 — a digest is a report, not a gate
        log.warning("tuning digest section failed for %s", session_dir.name, exc_info=True)
        return None
    return None if section is None else section.to_dict()


def compute_digest(
    session_dir: Path,
    config: CanaryConfig,
    *,
    now: datetime | None = None,
) -> SessionDigest:
    """Assemble the full digest for one completed trip.

    Raises ``FileNotFoundError`` when the session has no telemetry parquet
    (mirrors every other post-hoc sidecar in this doc — caller treats it as
    "skip", not "quarantine").
    """
    session_id = session_dir.name
    end = datetime.now(UTC) if now is None else now
    start = _session_start(session_dir) or end

    try:
        classification = classify_session(session_dir, config)
        duration_seconds = classification.duration_seconds
        mode_seconds = dict(classification.mode_seconds)
    except FileNotFoundError:
        duration_seconds = 0.0
        mode_seconds = {}

    stats = compute_fingerprint(session_dir, config)
    reference = read_reference_epoch(config.daemon.reference_epoch)
    reference_stats = dict(reference.stats) if reference is not None else {}

    anomaly_counts = _anomaly_counts_by_source(config.daemon.anomalies_dir, start, end)
    ledger = VehicleLedgerStore(config.daemon.vehicle_ledger)

    return SessionDigest(
        schema_version=SCHEMA_VERSION,
        computed_at=_format_timestamp(end),
        session_id=session_id,
        duration_seconds=duration_seconds,
        mode_seconds=mode_seconds,
        anomaly_counts_by_source=anomaly_counts,
        top_behavior_deviations=_top_behavior_deviations(stats),
        fingerprint_top_movers=_fingerprint_top_movers(stats, reference_stats),
        new_dtcs=_new_dtcs(session_dir),
        new_ledger_events=_new_ledger_events(ledger, start, end),
        tuning=_tuning_section(session_dir, config),
    )


def write_digest(session_dir: Path, digest: SessionDigest) -> Path:
    """Atomic tmp+rename write, matching ``classification.json`` /
    ``fingerprint.json`` / ``dtc.json`` / ``trip_summary.json``."""
    path = session_dir / DIGEST_FILENAME
    atomic_write_text(
        path,
        json.dumps(digest.to_dict(), indent=2, sort_keys=True, default=_json_default) + "\n",
    )
    return path


def _json_default(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    return str(value)


def read_digest(session_dir: Path) -> dict[str, Any] | None:
    """Load the sidecar dict, or ``None`` if absent / unreadable."""
    path = session_dir / DIGEST_FILENAME
    if not path.exists():
        return None
    try:
        return dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        return None


def get_or_compute_digest(
    session_dir: Path,
    config: CanaryConfig,
    *,
    force: bool = False,
    now: datetime | None = None,
    catalog_db: Path | None = None,
) -> dict[str, Any] | None:
    """Serve the sidecar, computing + persisting it on first request.

    Mirrors ``trip_summary.get_or_compute_trip_summary`` — a trip normally
    only reaches this once, via the Task 5.4 trip-end hook, but this stays
    idempotent/on-demand too (e.g. ``GET /sessions/{id}/digest`` before the
    trip-end hook has fired, or a session with no GPS to trigger it at
    all). Returns ``None`` when the session has no telemetry recorded yet.
    ``catalog_db`` (Task 6.3), when given, re-indexes the session into the
    catalog right after a freshly-computed digest is written; ``None`` (the
    default) skips the catalog entirely.
    """
    if not session_dir.is_dir():
        return None
    if not force:
        cached = read_digest(session_dir)
        if cached is not None:
            return cached
    try:
        digest = compute_digest(session_dir, config, now=now)
    except FileNotFoundError:
        return None
    write_digest(session_dir, digest)
    _update_catalog(catalog_db, session_dir)
    return read_digest(session_dir)


def _update_catalog(catalog_db: Path | None, session_dir: Path) -> None:
    """Best-effort catalog re-index (Task 6.3) — never blocks the digest
    write it rides along with. ``None`` (the default) skips it entirely."""
    if catalog_db is None:
        return
    try:
        from src.storage.catalog import SessionCatalog

        with SessionCatalog(catalog_db) as catalog:
            catalog.record_session(session_dir)
    except Exception:  # noqa: BLE001 — see docstring
        log.exception("catalog update failed for %s", session_dir.name)


__all__ = [
    "DIGEST_FILENAME",
    "SCHEMA_VERSION",
    "SessionDigest",
    "compute_digest",
    "get_or_compute_digest",
    "read_digest",
    "write_digest",
]

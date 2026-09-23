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
from contextlib import suppress
from dataclasses import dataclass, field
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
from src.preprocessing.loader import count_session_rows, resample_and_segment
from src.timefmt import format_timestamp as _format_timestamp
from src.timefmt import parse_iso_utc

log = logging.getLogger(__name__)

DIGEST_FILENAME = "digest.json"
#: 2 (A06.5): ``anomaly_counts_by_source`` counts alert-role events only and
#: ``evidence_counts_by_source`` counts the rest. A schema-1 sidecar is still
#: accurate as written — detector roles did not exist when it was computed,
#: so every event it counted was an alert — and readers treat its missing
#: evidence block as empty. It is deliberately *not* recomputed: the digest
#: window ends at "now" when computed after the fact, so a late recompute
#: would count events from long after the trip.
SCHEMA_VERSION = 2

#: Refusal sidecar (D03.6). A digest that did not run leaves this instead of
#: nothing, so a reader can tell the three states apart: ``digest.json``
#: present = computed; ``digest_skipped.json`` present = refused/failed, with
#: the reason; neither = never attempted. Before this existed, a session the
#: daemon was OOM-killed computing looked exactly like a session nobody had
#: got round to.
DIGEST_SKIPPED_FILENAME = "digest_skipped.json"
SKIP_SCHEMA_VERSION = 1

#: The directory is replay output / quarantine / scratch, not a recording of
#: this vehicle (``src.control.sessions.is_session_dir_name``).
REASON_NOT_A_SESSION = "not_a_session"
#: More rows than ``daemon.digest_row_budget`` allows to be loaded at once.
REASON_OVER_ROW_BUDGET = "over_row_budget"
#: The child hit its ``RLIMIT_AS`` ceiling (or the machine's own limit).
REASON_CHILD_OUT_OF_MEMORY = "child_out_of_memory"
#: The child was still working when the timeout expired and was terminated.
REASON_CHILD_TIMED_OUT = "child_timed_out"
#: The child could not start, or died without answering.
REASON_CHILD_FAILED = "child_failed"
#: The daemon was stopping and cancelled the child before it finished
#: (D03.6). Recorded so the shutdown fallback does not start the same
#: multi-gigabyte work a second time inside the unit's stop deadline, and so
#: the next start can tell "interrupted" from "never attempted".
REASON_INTERRUPTED_AT_SHUTDOWN = "interrupted_at_shutdown"

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
    #: Alert-role events only (A06.5) — what "gated anomaly events" means.
    anomaly_counts_by_source: dict[str, int]
    top_behavior_deviations: list[dict[str, Any]]
    fingerprint_top_movers: list[dict[str, Any]]
    new_dtcs: list[dict[str, Any]]
    new_ledger_events: list[dict[str, Any]]
    #: Tuning section, present only for capture sessions. ``None`` on an
    #: ordinary drive rather than an empty block, so a reader can tell "not a
    #: tuning capture" from "a capture that found nothing".
    tuning: dict[str, Any] | None = None
    #: A06.5 — events from ``evidence``-role detectors, stored as low-weight
    #: evidence and never alerts. Kept apart so no reader can add them to the
    #: alert count.
    evidence_counts_by_source: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "computed_at": self.computed_at,
            "session_id": self.session_id,
            "duration_seconds": self.duration_seconds,
            "mode_seconds": dict(self.mode_seconds),
            "anomaly_counts_by_source": dict(self.anomaly_counts_by_source),
            "evidence_counts_by_source": dict(self.evidence_counts_by_source),
            "top_behavior_deviations": list(self.top_behavior_deviations),
            "fingerprint_top_movers": list(self.fingerprint_top_movers),
            "new_dtcs": list(self.new_dtcs),
            "new_ledger_events": list(self.new_ledger_events),
            "tuning": self.tuning,
        }


@dataclass(frozen=True)
class DigestSkipped:
    """Why a digest was *not* produced for a session.

    Written whenever the computation is refused up front or fails in the
    child, so "the digest was skipped: 6,400,000 rows over the 4,000,000-row
    budget" and "the digest was never attempted" are different things on
    disk. ``row_count`` is the Parquet footer count (no data read), ``None``
    when nothing readable was there to count.
    """

    schema_version: int
    session_id: str
    #: Machine-readable kind — one of the ``REASON_*`` constants above.
    reason: str
    #: Human-readable one-liner naming the numbers behind ``reason``.
    detail: str
    recorded_at: str
    row_count: int | None
    row_budget: int | None
    #: Which digest code refused, mirroring ``classification.json``'s
    #: ``classifier_version``: a later schema may compute what this one
    #: declined, and a reader must be able to see the refusal is stale.
    digest_schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "reason": self.reason,
            "detail": self.detail,
            "recorded_at": self.recorded_at,
            "row_count": self.row_count,
            "row_budget": self.row_budget,
            "digest_schema_version": self.digest_schema_version,
        }


def _skipped(
    session_dir: Path,
    reason: str,
    detail: str,
    *,
    row_count: int | None,
    row_budget: int | None,
    now: datetime | None = None,
) -> DigestSkipped:
    return DigestSkipped(
        schema_version=SKIP_SCHEMA_VERSION,
        session_id=session_dir.name,
        reason=reason,
        detail=detail,
        recorded_at=_format_timestamp(datetime.now(UTC) if now is None else now),
        row_count=row_count,
        row_budget=row_budget,
    )


def digest_preflight(
    session_dir: Path,
    config: CanaryConfig,
    *,
    now: datetime | None = None,
) -> DigestSkipped | None:
    """The gate every digest computation passes, or ``None`` to go ahead.

    Two refusals, both cheap (a name match and Parquet footer metadata — no
    data read), and both here rather than at a call site because the caller
    that forgot them is the one that took the daemon down:

    * the directory is not a session (``_replay-*`` and friends) — the HTTP
      read has always refused those via ``resolve_session``; the trip-end
      hook did not, and computed a digest over 5.9 M replayed rows;
    * the session holds more rows than ``daemon.digest_row_budget``. Peak
      memory is O(rows x channels) because the pivot runs before the
      resample: 5,483 MB measured for 2,622,370 rows, and the 8 GB Pi's
      cliff is bracketed between ~4.5 M rows (completed) and ~5.9 M rows
      (OOM-killed). The budget refuses instead of trying and dying.
    """
    from src.control.sessions import is_session_dir_name
    from src.control.trip_end import digest_row_budget

    # Derived from the child's address-space ceiling when unset, so the budget
    # and the ceiling cannot state different limits (see digest_row_budget).
    budget = digest_row_budget(config)
    if not is_session_dir_name(session_dir.name):
        return _skipped(
            session_dir,
            REASON_NOT_A_SESSION,
            f"{session_dir.name!r} is not a live session or capture directory",
            row_count=None,
            row_budget=budget or None,
            now=now,
        )
    rows = count_session_rows(session_dir)
    if budget > 0 and rows is not None and rows > budget:
        return _skipped(
            session_dir,
            REASON_OVER_ROW_BUDGET,
            f"{rows:,} rows over the {budget:,}-row digest budget",
            row_count=rows,
            row_budget=budget,
            now=now,
        )
    return None


def write_digest_skipped(session_dir: Path, skipped: DigestSkipped) -> Path:
    """Persist the refusal sidecar (atomic tmp+rename, like every sibling)."""
    path = session_dir / DIGEST_SKIPPED_FILENAME
    atomic_write_text(
        path,
        json.dumps(skipped.to_dict(), indent=2, sort_keys=True, default=_json_default) + "\n",
    )
    log.warning(
        "digest skipped for %s: %s (%s)",
        skipped.session_id,
        skipped.detail,
        skipped.reason,
        extra={"digest_skip_reason": skipped.reason, "digest_row_count": skipped.row_count},
    )
    return path


def read_digest_skipped(session_dir: Path) -> dict[str, Any] | None:
    """Load the refusal sidecar, or ``None`` if absent / unreadable."""
    path = session_dir / DIGEST_SKIPPED_FILENAME
    if not path.exists():
        return None
    try:
        return dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        return None


def clear_digest_skipped(session_dir: Path) -> None:
    """Drop a stale refusal once a digest has actually been written.

    A directory must never hold both: "computed" and "refused" are mutually
    exclusive answers, and a leftover refusal beside a real ``digest.json``
    is the contradiction a reader would have to arbitrate.
    """
    with suppress(OSError):
        (session_dir / DIGEST_SKIPPED_FILENAME).unlink(missing_ok=True)


def _session_start(session_dir: Path) -> datetime | None:
    from src.control.sessions import session_started_at

    return session_started_at(session_dir)


def _anomaly_counts_by_source(
    anomalies_dir: Path, start: datetime, end: datetime
) -> tuple[dict[str, int], dict[str, int]]:
    """Count gated anomaly events whose timestamp falls in ``[start, end]``,
    as ``(alerts, evidence)`` by source.

    A06.5: an event from an ``evidence``-role detector is not an alert, so it
    is counted in the second mapping and never in the first.

    Only reads the specific per-day files the window can possibly span
    (daily rotation, see ``event_log_path``) rather than every file the
    anomalies directory has ever accumulated.
    """
    counts: dict[str, int] = {}
    evidence: dict[str, int] = {}
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
                    bucket = evidence if event.role == "evidence" else counts
                    bucket[event.source] = bucket.get(event.source, 0) + 1
        day += _ONE_DAY
    return counts, evidence


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

    segmented = resample_and_segment([session_dir], config, value="value_number")
    try:
        classification = classify_session(session_dir, config, segmented=segmented)
        duration_seconds = classification.duration_seconds
        mode_seconds = dict(classification.mode_seconds)
    except FileNotFoundError:
        duration_seconds = 0.0
        mode_seconds = {}

    stats = compute_fingerprint(session_dir, config, segmented=segmented)
    reference = read_reference_epoch(config.daemon.reference_epoch)
    reference_stats = dict(reference.stats) if reference is not None else {}

    anomaly_counts, evidence_counts = _anomaly_counts_by_source(
        config.daemon.anomalies_dir, start, end
    )
    ledger = VehicleLedgerStore(config.daemon.vehicle_ledger)

    return SessionDigest(
        schema_version=SCHEMA_VERSION,
        computed_at=_format_timestamp(end),
        session_id=session_id,
        duration_seconds=duration_seconds,
        mode_seconds=mode_seconds,
        anomaly_counts_by_source=anomaly_counts,
        evidence_counts_by_source=evidence_counts,
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


def persist_digest(
    session_dir: Path,
    digest: SessionDigest,
    *,
    catalog_db: Path | None = None,
) -> dict[str, Any] | None:
    """Write a freshly-computed digest and everything that rides along.

    The single write path for both the in-process and the off-loop
    (child-process) computation, so a digest produced either way lands
    byte-identically and clears any earlier refusal.
    """
    write_digest(session_dir, digest)
    clear_digest_skipped(session_dir)
    _update_catalog(catalog_db, session_dir)
    return read_digest(session_dir)


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

    Returns ``None`` both when there is nothing to digest and when
    :func:`digest_preflight` refuses — but a refusal is not silent: it lands
    in ``digest_skipped.json`` (:func:`read_digest_skipped`), which is what
    separates "over the row budget" from "never attempted".
    """
    if not session_dir.is_dir():
        return None
    if not force:
        cached = read_digest(session_dir)
        if cached is not None:
            return cached
    skipped = digest_preflight(session_dir, config, now=now)
    if skipped is not None:
        write_digest_skipped(session_dir, skipped)
        return None
    try:
        digest = compute_digest(session_dir, config, now=now)
    except FileNotFoundError:
        return None
    return persist_digest(session_dir, digest, catalog_db=catalog_db)


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
    "DIGEST_SKIPPED_FILENAME",
    "REASON_CHILD_FAILED",
    "REASON_CHILD_OUT_OF_MEMORY",
    "REASON_CHILD_TIMED_OUT",
    "REASON_NOT_A_SESSION",
    "REASON_OVER_ROW_BUDGET",
    "SCHEMA_VERSION",
    "SKIP_SCHEMA_VERSION",
    "DigestSkipped",
    "SessionDigest",
    "clear_digest_skipped",
    "compute_digest",
    "digest_preflight",
    "get_or_compute_digest",
    "persist_digest",
    "read_digest",
    "read_digest_skipped",
    "write_digest",
    "write_digest_skipped",
]

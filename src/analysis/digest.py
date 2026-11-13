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
* new ledger events <- :class:`~src.vehicle.vehicle_ledger.VehicleLedgerStore`
  entries recorded during the session's time window
"""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import timedelta as _timedelta
from pathlib import Path
from typing import Any

from src.alerts.event_logger import event_log_path, read_events
from src.analysis.digest_admission import (
    _TOP_LEVEL_LOCK,
    DIGEST_CHAIN_BYTES_PER_SECOND,
    DIGEST_CHAIN_FIXED_BYTES,
    DIGEST_PEAK_BYTES_PER_ROW,
    REASON_CHILD_FAILED,
    REASON_CHILD_OUT_OF_MEMORY,
    REASON_CHILD_TIMED_OUT,
    REASON_INTERRUPTED_AT_SHUTDOWN,
    REASON_NO_SPEED_SOURCE,
    REASON_NOT_A_SESSION,
    REASON_OVER_ROW_BUDGET,
    REASON_OVER_SPAN_BUDGET,
    SCHEMA_VERSION,
    SKIP_SCHEMA_VERSION,
    TOP_LEVEL_RANK_KEY,
    DigestBudgets,
    DigestSkipped,
    _clear_top_level_skipped,
    _file_away_current_answer,
    _top_level_may_take,
    _tripless_rank,
    clear_digest_skipped,
    derived_address_space_bytes,
    digest_address_space_bytes,
    digest_preflight,
    digest_row_budget,
    digest_span_budget_seconds,
    write_digest_skipped,
)
from src.analysis.fingerprint import compute_fingerprint
from src.analysis.reference_epoch import read_reference_epoch
from src.analysis.rolling_circumference import measure_session, write_rolling_circumference
from src.analysis.session_classifier import classify_session
from src.config import CanaryConfig
from src.jsonable import json_default
from src.preprocessing.loader import resample_and_segment
from src.session import DTC_SIDECAR_FILENAME
from src.storage.catalog import record_session_best_effort
from src.storage.session_products import (
    DIGEST_FILENAME,
    read_digest,
    trip_digest_path,
    trip_digest_skipped_path,
    trip_key,
    trip_started_at,
)
from src.storage.sessions import session_started_at
from src.storage.sidecar import read_json_object, write_json_sidecar
from src.timefmt import format_timestamp as _format_timestamp
from src.timefmt import parse_iso_utc
from src.vehicle.vehicle_ledger import VehicleLedgerStore

log = logging.getLogger(__name__)


#: How many entries to keep in the compact "top movers" / "top deviations"
#: lists — a digest is meant to be skimmed (and pushed through a
#: notification), not a full report; that's what fingerprint.json /
#: trip_summary.json already are.
_TOP_N = 5

#: Prefix fingerprint stats append for behavior-surface deviations
#: (mirrors ``src.behavior.behavior_decoder.DEVIATION_COLUMN_PREFIX`` stripped
#: of its ``deviation:`` wide-column form — see
#: ``fingerprint._add_behavior_deviation_stats``).
_BEHAVIOR_DEVIATION_STAT_PREFIX = "behavior_deviation_abs_mean:"

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
    #: tuning capture" from "a capture that found nothing"; a section that
    #: crashed reads ``{"available": False, "kind": "failed", ...}``.
    tuning: dict[str, Any] | None = None
    #: A06.5 — events from ``evidence``-role detectors, stored as low-weight
    #: evidence and never alerts. Kept apart so no reader can add them to the
    #: alert count.
    evidence_counts_by_source: dict[str, int] = field(default_factory=dict)
    #: D03.29 — the trip this digest closes, when the trip-end edge produced
    #: it: start/stop times and every session the trip spanned, ordered by
    #: parsed start time (``src.control.trip_end.trip_block``). The digest's
    #: statistics cover *this* session only; the block is what keeps a
    #: capture started mid-drive, or a restart, from silently dropping the
    #: rest of the drive. ``None`` for an on-demand or shutdown-less digest.
    trip: dict[str, Any] | None = None
    #: The driven tires' rolling circumference measured over this drive's
    #: steady cruise (:mod:`src.analysis.rolling_circumference`), or why there
    #: is none. Computed here because the digest already reads the session in
    #: its bounded child, but **not part of ``digest.json``**: :func:`write_digest`
    #: stores it as its own ``rolling_circumference.json``, the one place
    #: :mod:`src.control.tires` reads, so a drive refused a digest can still
    #: carry one.
    rolling_circumference: dict[str, Any] | None = None

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
            "trip": self.trip,
        }


def _session_start(session_dir: Path) -> datetime | None:

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
    return read_json_object(session_dir / DTC_SIDECAR_FILENAME)


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

    Never raises but ``MemoryError``: a normal drive's digest must not fail
    because the tuning section had nothing to say, or because a vehicle has no
    AFR calibration; running out of memory is the child's refusal to report.
    But "not a capture" and "the section crashed" are different answers
    (R05.12): a crash is reported as ``{"available": False, "kind":
    "failed", "error": ...}``, never as the ``None`` an ordinary drive gets.
    """
    from src.analysis.tuning_digest import compute_tuning_digest

    try:
        section = compute_tuning_digest(session_dir, config)
    except MemoryError:
        # Out of memory is the digest child's to report (child_out_of_memory,
        # src.control.trip_end), never an absent channel or a failed section:
        # swallowed here, a capture over the ceiling read as "inputs never
        # captured" (P02.1, 2026-09-26).
        raise
    except Exception as exc:  # noqa: BLE001 — a digest is a report, not a gate
        log.warning("tuning digest section failed for %s", session_dir.name, exc_info=True)
        return {"available": False, "kind": "failed", "error": f"{type(exc).__name__}: {exc}"}
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
        rolling_circumference=measure_session(session_dir, config),
    )


def write_digest(session_dir: Path, digest: SessionDigest) -> Path:
    """Atomic tmp+rename write, matching ``classification.json`` /
    ``fingerprint.json`` / ``dtc.json`` / ``trip_summary.json``.

    Filed under its trip in ``trip_digests/`` and, when this trip is not older
    than the one the top level already holds (:func:`_top_level_may_take`),
    mirrored to ``digest.json`` with the top-level refusal removed, all under
    one lock so no reader sees both. The trip's own refusal, if any, goes too.
    A whole-session digest that replaces a real trip's refusal carries that
    trip's start as its rank (:data:`TOP_LEVEL_RANK_KEY`).
    """
    path = session_dir / DIGEST_FILENAME
    key = trip_key(digest.trip)
    payload = digest.to_dict()
    with _TOP_LEVEL_LOCK:
        if trip_started_at(digest.trip) is None:
            rank = _tripless_rank(session_dir)
            if rank is not None:
                payload[TOP_LEVEL_RANK_KEY] = rank
        _file_away_current_answer(session_dir)
        write_json_sidecar(
            trip_digest_path(session_dir, key), payload, default=json_default, mkdir=True
        )
        with suppress(OSError):
            trip_digest_skipped_path(session_dir, key).unlink(missing_ok=True)
        if _top_level_may_take(session_dir, digest.trip, is_digest=True):
            write_json_sidecar(path, payload, default=json_default)
            _clear_top_level_skipped(session_dir)
    if digest.rolling_circumference is not None:
        write_rolling_circumference(session_dir, digest.rolling_circumference)
    return path


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

    record_session_best_effort(catalog_db, session_dir)
    return read_digest(session_dir)


def tuning_section(session_dir: Path, config: CanaryConfig) -> dict[str, Any] | None:
    """:func:`_tuning_section` for the digest child's tuning refresh (R05.16)."""
    return _tuning_section(session_dir, config)


def stale_tuning_version(payload: dict[str, Any]) -> int | None:
    """The stored tuning section's schema version when it predates
    :data:`~src.analysis.tuning_digest.TUNING_SCHEMA_VERSION`, else ``None``.

    Nothing re-reads a digest once it is written (A06.5), so a section
    computed by an older engine stays on disk: a section without
    ``schema_version`` is 1, the 1 s last-cell delivery engine, whose numbers
    are not comparable with the acquisition-paired 2 (R05.16). Only the tuning
    section may be refreshed. It reads nothing but the session, whereas a
    whole recompute's anomaly window would end at "now" (A06.5). A recorded
    section failure is a failure, not stale numbers, and is not retried here.
    """
    from src.analysis.tuning_digest import TUNING_SCHEMA_VERSION

    section = payload.get("tuning")
    if not isinstance(section, dict) or section.get("kind") == "failed":
        return None
    version = section.get("schema_version", 1)
    if not isinstance(version, int) or version >= TUNING_SCHEMA_VERSION:
        return None
    return version


def write_refreshed_tuning(
    session_dir: Path,
    payload: dict[str, Any],
    section: dict[str, Any] | None,
    *,
    from_version: int,
    now: datetime | None = None,
    catalog_db: Path | None = None,
) -> dict[str, Any] | None:
    """Replace a stale tuning section in ``digest.json``, and say so.

    Everything else in the stored digest is kept as written at trip end.
    ``tuning_refreshed`` records which section version was replaced and
    when, so a refreshed digest never passes for one computed whole.
    """
    from src.analysis.tuning_digest import TUNING_SCHEMA_VERSION

    refreshed = {
        **payload,
        "tuning": section,
        "tuning_refreshed": {
            "from_schema_version": from_version,
            "to_schema_version": TUNING_SCHEMA_VERSION,
            "refreshed_at": _format_timestamp(datetime.now(UTC) if now is None else now),
        },
    }
    key = trip_key(refreshed.get("trip"))
    with _TOP_LEVEL_LOCK:
        held = read_digest(session_dir)
        # A newer trip's answer may have landed since ``payload`` was read; the
        # top level then is not this trip's to rewrite.
        if held is not None and trip_key(held.get("trip")) == key:
            write_json_sidecar(session_dir / DIGEST_FILENAME, refreshed, default=json_default)
        stored = trip_digest_path(session_dir, key)
        if stored.exists():
            write_json_sidecar(stored, refreshed, default=json_default)
    record_session_best_effort(catalog_db, session_dir)
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
        write_digest_skipped(session_dir, skipped, catalog_db=catalog_db)
        return None
    try:
        digest = compute_digest(session_dir, config, now=now)
    except FileNotFoundError:
        return None
    return persist_digest(session_dir, digest, catalog_db=catalog_db)


__all__ = [
    "DIGEST_CHAIN_BYTES_PER_SECOND",
    "DIGEST_CHAIN_FIXED_BYTES",
    "DIGEST_PEAK_BYTES_PER_ROW",
    "REASON_CHILD_FAILED",
    "REASON_CHILD_OUT_OF_MEMORY",
    "REASON_CHILD_TIMED_OUT",
    "REASON_INTERRUPTED_AT_SHUTDOWN",
    "REASON_NOT_A_SESSION",
    "REASON_NO_SPEED_SOURCE",
    "REASON_OVER_ROW_BUDGET",
    "REASON_OVER_SPAN_BUDGET",
    "SCHEMA_VERSION",
    "SKIP_SCHEMA_VERSION",
    "TOP_LEVEL_RANK_KEY",
    "DigestBudgets",
    "DigestSkipped",
    "SessionDigest",
    "clear_digest_skipped",
    "compute_digest",
    "derived_address_space_bytes",
    "digest_address_space_bytes",
    "digest_preflight",
    "digest_row_budget",
    "digest_span_budget_seconds",
    "get_or_compute_digest",
    "persist_digest",
    "write_digest",
    "write_digest_skipped",
]

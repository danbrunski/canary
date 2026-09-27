"""Daemon-side helpers for serving session classification sidecars.

The classifier itself (:func:`src.analysis.session_classifier.classify_session`)
is pure; this module wraps it for the control-plane HTTP endpoints + MCP
tools. The shape we expose mirrors :class:`SessionClassification.to_dict`
plus a few aggregate views useful for dashboards / agents.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.analysis.fingerprint import compute_fingerprint, write_fingerprint
from src.analysis.session_classifier import classify_session, write_classification
from src.config import CanaryConfig
from src.control.trends import evaluate_and_alert_trends
from src.session import LIVE_SESSION_KINDS, SessionKind, iter_sessions, session_kind
from src.storage.catalog import record_session_best_effort
from src.storage.session_products import DERIVED_TAGS_FILENAME, read_classification

if TYPE_CHECKING:
    from src.alerts.emitter import AnomalyEventEmitter

log = logging.getLogger(__name__)


def _iter_session_dirs(storage_path: Path) -> Iterable[Path]:
    """Plain live sessions, oldest first by parsed start time (R01.1)."""
    return iter_sessions(storage_path, kinds=LIVE_SESSION_KINDS)


def get_session_classification(storage_path: Path, session_id: str) -> dict[str, Any] | None:
    """Return the sidecar JSON for one session, or ``None`` when absent.

    The session id is validated against the live-session regex so a caller
    can't request ``_replay-*`` or ``_archive``; those have their own
    flows.
    """
    if session_kind(session_id) is not SessionKind.LIVE:
        return None
    session_dir = storage_path / session_id
    if not session_dir.is_dir():
        return None
    classification = read_classification(session_dir)
    return None if classification is None else classification.to_dict()


def classify_and_write(config: CanaryConfig, session_id: str) -> dict[str, Any]:
    """The whole-session half of a classify pass: classify, fingerprint, write.

    This is the part that reads the session (``resample_and_segment``, then
    ``compute_fingerprint`` — part by part since P02.1, a peak that follows
    the session's span: ``DIGEST_CHAIN_BYTES_PER_SECOND`` in
    ``analysis/digest.py``), so the daemon runs it in the bounded
    single-flight child (``workbench_jobs`` kind ``classify``, R03.2). It
    returns only what the parent needs: the classification payload, whether
    the fingerprint sidecar was written, and the session directory. Nothing
    here touches the emitter or the catalog.

    Raises ``ValueError`` for invalid session ids or non-live names;
    ``FileNotFoundError`` when the session has no telemetry parquet.
    """
    if session_kind(session_id) is not SessionKind.LIVE:
        raise ValueError(f"refusing to classify non-live-session id {session_id!r}")
    session_dir = config.collection.storage_path / session_id
    if not session_dir.is_dir():
        raise FileNotFoundError(f"session {session_id!r} not found under {session_dir.parent}")
    classification = classify_session(session_dir, config)
    write_classification(session_dir, classification)
    # Fingerprint (Task 4.1/4.2) rides the same on-demand classify pass —
    # one post-session compute, not a separate corpus walk.
    fingerprint_written = False
    try:
        fingerprint_stats = compute_fingerprint(session_dir, config)
        write_fingerprint(session_dir, fingerprint_stats)
        fingerprint_written = True
    except Exception:  # noqa: BLE001 — fingerprinting must not block classification
        log.exception("fingerprint failed for %s", session_id)
    return {
        "classification": classification.to_dict(),
        "fingerprint_written": fingerprint_written,
        "session_dir": str(session_dir),
    }


def finish_classification(
    config: CanaryConfig,
    session_id: str,
    written: dict[str, Any],
    *,
    emitter: AnomalyEventEmitter | None = None,
    catalog_db: Path | None = None,
) -> dict[str, Any]:
    """The parent half: trend alerting and the catalog, from sidecars only.

    ``written`` is :func:`classify_and_write`'s return value. Runs in the
    daemon process because the emitter is the live (broadcasting) one; it
    reads sidecars, never the session's telemetry.
    """
    if written.get("fingerprint_written"):
        # Trend evaluation + alerting (Task 4.4/4.6) rides the same pass, right
        # after the fingerprint that feeds it — never blocks classification.
        try:
            evaluate_and_alert_trends(config, session_id, emitter=emitter)
        except Exception:  # noqa: BLE001 — trend alerting must not block classification
            log.exception("trend evaluation failed for %s", session_id)
    record_session_best_effort(catalog_db, Path(written["session_dir"]))
    payload: dict[str, Any] = written["classification"]
    return payload


def classify_one_session(
    config: CanaryConfig,
    session_id: str,
    *,
    emitter: AnomalyEventEmitter | None = None,
    catalog_db: Path | None = None,
) -> dict[str, Any]:
    """Run the classifier on one session and persist the sidecar, in-process.

    :func:`classify_and_write` then :func:`finish_classification`. For
    scripts and tests; the daemon route runs the first half in the bounded
    child instead (R03.2).

    Raises ``ValueError`` for invalid session ids or non-live names;
    ``FileNotFoundError`` when the session has no telemetry parquet.
    ``emitter`` (Task 4.6), when given, is the live daemon's real
    (broadcasting) emitter so a trend alert reaches the dashboard's live
    stream, not just the persisted log; ``None`` falls back to a standalone
    one (see ``evaluate_and_alert_trends``).
    ``catalog_db`` (Task 6.3), when given, is the session catalog's DB path —
    the classification + fingerprint sidecars just written are indexed into
    it right after; ``None`` (the default) skips the catalog entirely so
    callers that never wire one up never touch that file.
    """
    written = classify_and_write(config, session_id)
    return finish_classification(
        config, session_id, written, emitter=emitter, catalog_db=catalog_db
    )


def corpus_mode_summary(storage_path: Path) -> dict[str, Any]:
    """Aggregate mode-coverage hours across every classified session.

    The result block looks like::

        {
          "classified_session_count": 24,
          "unclassified_session_count": 1,
          "mode_seconds": {"warmup": 1820.0, "idle": 9230.0, ...},
          "event_counts": {"cold_start": 6, "hard_accel": 12, ...},
          "by_disposition": {"ready": 22, "review": 1, "quarantine": 1}
        }

    "Classified" means the sidecar exists. "Unclassified" means a
    well-formed live-session directory under ``storage_path`` has no
    sidecar. ``label_sessions.py --all`` is the way to close that gap.
    """
    classified = 0
    unclassified = 0
    mode_totals: dict[str, float] = {}
    event_totals: dict[str, int] = {}
    by_disposition: dict[str, int] = {"ready": 0, "review": 0, "quarantine": 0}
    for session_dir in _iter_session_dirs(storage_path):
        classification = read_classification(session_dir)
        if classification is None:
            unclassified += 1
            continue
        classified += 1
        for mode, seconds in classification.mode_seconds.items():
            mode_totals[mode] = mode_totals.get(mode, 0.0) + float(seconds)
        for event, count in classification.event_counts.items():
            event_totals[event] = event_totals.get(event, 0) + int(count)
        by_disposition[classification.disposition] = (
            by_disposition.get(classification.disposition, 0) + 1
        )
    return {
        "classified_session_count": classified,
        "unclassified_session_count": unclassified,
        "mode_seconds": {k: round(v, 2) for k, v in mode_totals.items()},
        "event_counts": event_totals,
        "by_disposition": by_disposition,
    }


def list_events_by_kind(
    storage_path: Path,
    *,
    kind: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Walk every session's ``derived_tags.jsonl`` and surface matching events.

    Used to answer "show me every hard_accel I've ever recorded" without
    re-running the classifier. Results are sorted newest-first and
    truncated at ``limit``.
    """
    if limit <= 0:
        return {"events": [], "count": 0, "truncated": False}
    collected: list[dict[str, Any]] = []
    for session_dir in _iter_session_dirs(storage_path):
        derived = session_dir / DERIVED_TAGS_FILENAME
        if not derived.exists():
            continue
        try:
            for line in derived.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kind is not None and record.get("label") != kind:
                    continue
                record.setdefault("session_id", session_dir.name)
                collected.append(record)
        except OSError:
            continue
    collected.sort(key=lambda record: record.get("timestamp", ""), reverse=True)
    truncated = len(collected) > limit
    return {
        "events": collected[:limit],
        "count": min(len(collected), limit),
        "truncated": truncated,
    }


__all__ = [
    "classify_one_session",
    "corpus_mode_summary",
    "get_session_classification",
    "list_events_by_kind",
]

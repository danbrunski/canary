"""Daemon-side helpers for serving + alerting on session-fingerprint drift
trends (Tasks 4.5/4.6, eval-longitudinal-and-dtc.md).

:mod:`src.analysis.trends` is pure numpy over an already-assembled
session-ordered series; this module does the I/O :mod:`src.control.classification`
does for the classifier — walking the on-disk sessions, reading fingerprint
sidecars + the frozen reference epoch, and (for 4.6) persisting the tiny
idempotency marker that keeps a re-evaluated session from double-alerting.

Two entry points:

* :func:`corpus_trend_summary` — the read side (Task 4.5): every stat the
  reference epoch knows about, evaluated over the full fingerprint history.
  Powers ``GET /control/trends`` (and through it, the MCP ``get_trends``
  tool + ``scripts/fingerprint.py trends``).
* :func:`evaluate_and_alert_trends` — the write side (Task 4.6): called
  right after a session's ``fingerprint.json`` is written (the same
  post-session hook as Task 4.2, in both
  :func:`src.control.classification.classify_one_session` and
  ``scripts/label_sessions.py``). Emits one ``AnomalyEvent(source="trend")``
  per stat newly found in sustained CUSUM alarm for THAT session.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger
from src.alerts.events import AnomalyEvent
from src.analysis.fingerprint import read_fingerprint
from src.analysis.reference_epoch import read_reference_epoch
from src.analysis.trends import evaluate_trends
from src.config import CanaryConfig
from src.timefmt import format_timestamp as _format_timestamp

log = logging.getLogger(__name__)

_SESSION_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}$")


def _iter_session_dirs(storage_path: Path) -> Iterable[Path]:
    if not storage_path.exists():
        return ()
    return (
        child
        for child in sorted(storage_path.iterdir())
        if child.is_dir() and _SESSION_DIR_RE.match(child.name)
    )


def _ordered_fingerprints(
    storage_path: Path, catalog_db: Path | None = None
) -> list[tuple[str, dict[str, float]]]:
    """Every session under ``storage_path`` that has a fingerprint, oldest-first
    (session directory names are ISO timestamps, so lexical sort == chronological
    order — same convention ``_iter_session_dirs`` already relies on).

    Task 6.4(c): prefer the catalog's ``fingerprint`` column over globbing
    sidecars directly, when the catalog is actually populated — an empty/
    never-built catalog (``catalog_db`` unset, missing, or built but empty)
    falls straight back to the sidecar walk, so this is a pure optimization
    once ``scripts/catalog.py rebuild`` has run, never a behavior change for
    a deployment that hasn't wired the catalog yet.
    """
    if catalog_db is not None and catalog_db.exists():
        try:
            from src.storage.catalog import SessionCatalog

            with SessionCatalog(catalog_db) as catalog:
                rows = catalog.fingerprints_by_session()
        except Exception:  # noqa: BLE001 — fall back to the sidecar walk below
            log.exception("catalog fingerprint read failed; falling back to sidecar walk")
            rows = []
        if rows:
            return [
                (session_id, stats)
                for session_id, stats in rows
                if _SESSION_DIR_RE.match(session_id) and (storage_path / session_id).is_dir()
            ]
    ordered: list[tuple[str, dict[str, float]]] = []
    for session_dir in _iter_session_dirs(storage_path):
        fingerprint = read_fingerprint(session_dir)
        if fingerprint is not None:
            ordered.append((session_dir.name, dict(fingerprint.stats)))
    return ordered


def corpus_trend_summary(
    config: CanaryConfig, *, sessions_root: Path | None = None
) -> dict[str, Any]:
    """Task 4.5 read side: per-stat latest value, reference band, trend
    verdict, CUSUM state — evaluated over the full fingerprint history.

    ``{"available": False, "reason": ...}`` when no reference epoch has been
    frozen yet (``scripts/fingerprint.py freeze``) — there's nothing to
    trend against.
    """
    reference = read_reference_epoch(config.daemon.reference_epoch)
    if reference is None:
        return {
            "available": False,
            "reason": f"no reference epoch frozen at {config.daemon.reference_epoch}",
            "stats": {},
        }
    root = sessions_root or config.collection.storage_path
    fingerprints = _ordered_fingerprints(root, config.daemon.catalog_db)
    trends = evaluate_trends(
        fingerprints,
        reference,
        cusum_k=config.trends.cusum_k,
        cusum_h=config.trends.cusum_h,
        mann_kendall_alpha=config.trends.mann_kendall_alpha,
    )
    return {
        "available": True,
        "frozen_at": reference.frozen_at,
        "reference_sessions": len(reference.sessions),
        "stats": {name: trend.to_dict() for name, trend in sorted(trends.items())},
    }


def _read_alert_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _write_alert_state(path: Path, state: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dict(state), indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def evaluate_and_alert_trends(
    config: CanaryConfig,
    session_id: str,
    *,
    sessions_root: Path | None = None,
    emitter: AnomalyEventEmitter | None = None,
) -> list[AnomalyEvent]:
    """Task 4.6 write side: emit a trend ``AnomalyEvent`` for each stat newly
    found in sustained CUSUM alarm as of ``session_id``.

    CUSUM state is recomputed from scratch over the full session-ordered
    fingerprint history on every call (Task 4.4's design note: the corpus is
    small and this only runs once per session, not per tick, so the extra
    compute is cheap and there's no incremental-state staleness/corruption
    risk). The only thing PERSISTED is a tiny idempotency marker
    (``daemon.trend_alert_state``: ``{stat: last_alerted_session_id}``), so
    re-running this for the same session (a retry, ``--rebuild``) never
    double-emits, while a LATER session that's still in alarm correctly gets
    its own fresh event — "a new alarm on a new session's fingerprint is a
    fresh event" (Task 4.6).

    No-op (``[]``) when there's no frozen reference epoch, or ``session_id``
    isn't the latest observation for any alarmed stat (e.g. the session
    carried none of the alarmed stats at all).

    ``emitter`` defaults to a standalone one over ``config.daemon.anomalies_dir``
    when not given — the offline ``label_sessions.py`` batch path has no live
    daemon emitter to hand in; the on-demand daemon route passes its real
    (broadcasting) one so the event also reaches the live dashboard stream.
    """
    reference = read_reference_epoch(config.daemon.reference_epoch)
    if reference is None:
        return []
    root = sessions_root or config.collection.storage_path
    fingerprints = _ordered_fingerprints(root, config.daemon.catalog_db)
    trends = evaluate_trends(
        fingerprints,
        reference,
        cusum_k=config.trends.cusum_k,
        cusum_h=config.trends.cusum_h,
        mann_kendall_alpha=config.trends.mann_kendall_alpha,
    )
    alarmed = {
        stat: trend
        for stat, trend in trends.items()
        if trend.latest_session == session_id and trend.cusum is not None and trend.cusum.alarm
    }
    if not alarmed:
        return []

    state_path = config.daemon.trend_alert_state
    state = _read_alert_state(state_path)
    to_emit = {stat: trend for stat, trend in alarmed.items() if state.get(stat) != session_id}
    if not to_emit:
        return []

    own_logger: AnomalyEventLogger | None = None
    active_emitter = emitter
    if active_emitter is None:
        own_logger = AnomalyEventLogger(config.daemon.anomalies_dir)
        active_emitter = AnomalyEventEmitter(own_logger)

    emitted: list[AnomalyEvent] = []
    timestamp = _format_timestamp(datetime.now(UTC))
    try:
        for stat, trend in to_emit.items():
            cusum_result = trend.cusum
            assert cusum_result is not None  # narrowed by the `alarmed` filter above
            assert cusum_result.direction is not None  # `alarm` implies a direction
            event = active_emitter.emit_trend(
                stat=stat,
                session=session_id,
                timestamp=timestamp,
                latest_value=trend.latest_value,
                reference_mean=trend.reference_mean,
                reference_std=trend.reference_std,
                cusum_sh=cusum_result.sh,
                cusum_sl=cusum_result.sl,
                cusum_h=cusum_result.h,
                direction=cusum_result.direction,
                mann_kendall_trend=trend.mann_kendall.trend if trend.mann_kendall else None,
                mann_kendall_p_value=trend.mann_kendall.p_value if trend.mann_kendall else None,
            )
            if event is not None:
                emitted.append(event)
            state[stat] = session_id
    finally:
        if own_logger is not None:
            own_logger.close()

    _write_alert_state(state_path, state)
    return emitted


__all__ = [
    "corpus_trend_summary",
    "evaluate_and_alert_trends",
]

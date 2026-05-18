"""Structured search over anomaly + MCP audit JSONL history.

Single tool, two record sources selected via ``kind``:

* ``"anomalies"`` — fans out across ``anomalies-YYYY-MM-DD.jsonl`` files in
  the configured anomalies directory. The filename carries the date so we
  can prune day-files outside ``[since, until]`` without parsing them.
* ``"audit"`` — scans the single MCP audit log written by
  :mod:`src.mcp_server.audit`.

Filters that don't apply to the chosen kind are ignored. The free-text
``contains`` match is applied to the raw JSON line *before* parsing, so it
catches fields a structured filter wouldn't (custom ``detail`` keys, free
text in audit ``payload``).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_ANOMALY_FILE_RE = re.compile(r"anomalies-(\d{4}-\d{2}-\d{2})\.jsonl$")
#: Default lookback window for anomaly searches without an explicit ``since``.
#: Long enough for "what's been going wrong this week?", short enough not to
#: stream months of history when the LLM asks a vague question.
_DEFAULT_LOOKBACK = timedelta(days=7)


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp; tolerate trailing ``Z``. ``None`` → ``None``."""
    if value is None:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _record_timestamp(record: dict[str, Any]) -> datetime | None:
    """Pull a UTC datetime out of ``record["timestamp"]``, or ``None``."""
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return _parse_iso(raw)
    except ValueError:
        return None


def _within(record: dict[str, Any], since: datetime | None, until: datetime | None) -> bool:
    """True iff the record falls inside ``[since, until]``. Missing ts ⇒ skip."""
    ts = _record_timestamp(record)
    if ts is None:
        return False
    if since is not None and ts < since:
        return False
    return not (until is not None and ts > until)


def _anomaly_filter(
    record: dict[str, Any],
    *,
    source: str | None,
    mode: str | None,
    min_score: float | None,
    max_score: float | None,
    affected_channel: str | None,
    session: str | None,
) -> bool:
    """Apply anomaly-specific structural filters."""
    if source is not None and record.get("source") != source:
        return False
    if mode is not None and record.get("operating_mode") != mode:
        return False
    score = record.get("score")
    if isinstance(score, int | float):
        if min_score is not None and score < min_score:
            return False
        if max_score is not None and score > max_score:
            return False
    elif min_score is not None or max_score is not None:
        # Filter explicitly requested a score bound; record has none.
        return False
    if session is not None and record.get("session") != session:
        return False
    if affected_channel is not None:
        channels = record.get("affected_channels") or []
        if isinstance(channels, list | tuple) and affected_channel not in channels:
            return False
        elif not isinstance(channels, list | tuple):
            return False
    return True


def _audit_filter(
    record: dict[str, Any],
    *,
    action: str | None,
    result: str | None,
    actor: str | None,
) -> bool:
    """Apply audit-specific structural filters."""
    if action is not None and record.get("action") != action:
        return False
    if result is not None and record.get("result") != result:
        return False
    if actor is not None:
        payload = record.get("payload") or {}
        if not isinstance(payload, dict) or payload.get("actor") != actor:
            return False
    return True


def _anomaly_files_in_range(
    directory: Path,
    *,
    since: datetime | None,
    until: datetime | None,
) -> list[Path]:
    """Subset of ``anomalies-YYYY-MM-DD.jsonl`` files whose date may overlap."""
    if not directory.exists():
        return []
    candidates: list[tuple[str, Path]] = []
    for path in directory.iterdir():
        match = _ANOMALY_FILE_RE.search(path.name)
        if match is None:
            continue
        candidates.append((match.group(1), path))
    if not candidates:
        return []
    since_date = since.date().isoformat() if since else None
    until_date = until.date().isoformat() if until else None
    kept: list[tuple[str, Path]] = []
    for day, path in candidates:
        if since_date is not None and day < since_date:
            continue
        if until_date is not None and day > until_date:
            continue
        kept.append((day, path))
    # Date-sorted so output is reproducible regardless of FS order.
    kept.sort(key=lambda dp: dp[0])
    return [path for _, path in kept]


def _iter_jsonl_lines(path: Path) -> list[tuple[str, dict[str, Any] | None]]:
    """Read a JSONL file, returning ``(raw_line, parsed_or_None)`` per row."""
    if not path.exists():
        return []
    out: list[tuple[str, dict[str, Any] | None]] = []
    for line in path.read_text(errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            out.append((stripped, None))
            continue
        out.append((stripped, parsed if isinstance(parsed, dict) else None))
    return out


def search_anomaly_log(
    *,
    kind: str = "anomalies",
    anomalies_dir: Path | None,
    audit_path: Path,
    since: str | None = None,
    until: str | None = None,
    contains: str | None = None,
    source: str | None = None,
    mode: str | None = None,
    min_score: float | None = None,
    max_score: float | None = None,
    affected_channel: str | None = None,
    session: str | None = None,
    action: str | None = None,
    result: str | None = None,
    actor: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Search anomaly day-files or the MCP audit log.

    Parameters
    ----------
    kind:
        ``"anomalies"`` (default) or ``"audit"``. Filters that don't apply
        to the chosen kind are silently ignored.
    anomalies_dir:
        Directory holding ``anomalies-YYYY-MM-DD.jsonl``; ``None`` short-
        circuits an anomalies search with ``ok=False``.
    audit_path:
        MCP audit log path (always required; the server always writes one).
    since / until:
        ISO-8601 bounds. Default: last 7 days for anomalies, unbounded for
        audit (audit logs are bounded by file size, not date range).
    contains:
        Substring match against the raw JSON line before parsing.
    source / mode / min_score / max_score / affected_channel / session:
        Anomaly-specific structural filters.
    action / result / actor:
        Audit-specific structural filters. ``actor`` matches
        ``payload.actor`` (``"mcp"`` distinguishes MCP-driven entries).
    limit:
        Maximum records returned. Newest first.

    Returns
    -------
    ``{"ok": bool, "kind": str, "count": N, "events": [...]}`` or
    ``{"ok": False, "error": "...", "events": [], "count": 0}``.
    """
    if kind not in ("anomalies", "audit"):
        return {
            "ok": False,
            "error": f"kind must be 'anomalies' or 'audit', got {kind!r}",
            "events": [],
            "count": 0,
        }
    try:
        since_dt = _parse_iso(since)
        until_dt = _parse_iso(until)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "events": [], "count": 0}

    if kind == "anomalies":
        if anomalies_dir is None:
            return {
                "ok": False,
                "error": "anomalies search requires CANARY_ANOMALIES_DIR",
                "events": [],
                "count": 0,
            }
        if since_dt is None:
            since_dt = datetime.now(UTC) - _DEFAULT_LOOKBACK
        files = _anomaly_files_in_range(anomalies_dir, since=since_dt, until=until_dt)
    else:
        files = [audit_path]

    matched: list[dict[str, Any]] = []
    for path in files:
        for raw_line, record in _iter_jsonl_lines(path):
            if contains is not None and contains not in raw_line:
                continue
            if record is None:
                # Free-text contains can still match malformed lines; surface
                # them so a debugger sees corruption rather than silent drops.
                if contains is not None:
                    matched.append({"raw": raw_line})
                continue
            if not _within(record, since_dt, until_dt):
                continue
            if kind == "anomalies" and not _anomaly_filter(
                record,
                source=source,
                mode=mode,
                min_score=min_score,
                max_score=max_score,
                affected_channel=affected_channel,
                session=session,
            ):
                continue
            if kind == "audit" and not _audit_filter(
                record, action=action, result=result, actor=actor
            ):
                continue
            matched.append(record)

    # Newest-first. ``_record_timestamp`` returns ``None`` for raw lines —
    # they sort to the end so structured matches lead.
    matched.sort(
        key=lambda r: _record_timestamp(r) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    if limit > 0:
        matched = matched[:limit]
    return {"ok": True, "kind": kind, "count": len(matched), "events": matched}


__all__ = ["search_anomaly_log"]

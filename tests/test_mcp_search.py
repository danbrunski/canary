"""Tests for the ``search_anomaly_log`` tool."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.mcp_server.tools.search import search_anomaly_log


def _anomaly_event(**overrides: Any) -> dict[str, Any]:
    """Minimal ``AnomalyEvent``-shaped dict for fixtures."""
    base: dict[str, Any] = {
        # Relative so fixtures never age out of search_anomaly_log's default
        # 7-day lookback — a hardcoded date silently broke these tests a week
        # after it was written. Tests pinning a specific date override it.
        "timestamp": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        "source": "isolation_forest",
        "model_version": "1.0",
        "score": 0.5,
        "threshold": None,
        "affected_channels": [],
        "operating_mode": "idle",
        "session": "S1",
        "detail": {},
    }
    base.update(overrides)
    return base


def _write_anomalies(directory: Path, date: str, events: list[dict[str, Any]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"anomalies-{date}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _write_audit(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


# ---------------------------------------------------------------------------
# Anomalies search
# ---------------------------------------------------------------------------


def test_anomalies_search_returns_all_within_default_lookback(tmp_path: Path) -> None:
    """Without ``since``, the 7-day window is applied automatically."""
    today = datetime.now(UTC).date().isoformat()
    _write_anomalies(
        tmp_path,
        today,
        [
            _anomaly_event(timestamp=f"{today}T01:00:00+00:00", session="A"),
            _anomaly_event(timestamp=f"{today}T02:00:00+00:00", session="B"),
        ],
    )
    out = search_anomaly_log(
        kind="anomalies",
        anomalies_dir=tmp_path,
        audit_path=tmp_path / "audit.jsonl",
    )
    assert out["ok"] is True
    assert out["count"] == 2
    # Newest-first ordering.
    assert out["events"][0]["session"] == "B"


def test_anomalies_search_filters_by_source_and_mode(tmp_path: Path) -> None:
    today = datetime.now(UTC).date().isoformat()
    _write_anomalies(
        tmp_path,
        today,
        [
            _anomaly_event(source="isolation_forest", operating_mode="idle"),
            _anomaly_event(source="lstm_autoencoder", operating_mode="idle"),
            _anomaly_event(source="isolation_forest", operating_mode="highway"),
        ],
    )
    out = search_anomaly_log(
        kind="anomalies",
        anomalies_dir=tmp_path,
        audit_path=tmp_path / "audit.jsonl",
        source="isolation_forest",
        mode="idle",
    )
    assert out["count"] == 1
    assert out["events"][0]["source"] == "isolation_forest"
    assert out["events"][0]["operating_mode"] == "idle"


def test_anomalies_search_filters_by_score_range(tmp_path: Path) -> None:
    today = datetime.now(UTC).date().isoformat()
    _write_anomalies(
        tmp_path,
        today,
        [
            _anomaly_event(score=0.1),
            _anomaly_event(score=0.5),
            _anomaly_event(score=0.9),
        ],
    )
    out = search_anomaly_log(
        kind="anomalies",
        anomalies_dir=tmp_path,
        audit_path=tmp_path / "audit.jsonl",
        min_score=0.4,
        max_score=0.8,
    )
    assert out["count"] == 1
    assert out["events"][0]["score"] == 0.5


def test_anomalies_search_filters_by_affected_channel(tmp_path: Path) -> None:
    today = datetime.now(UTC).date().isoformat()
    _write_anomalies(
        tmp_path,
        today,
        [
            _anomaly_event(affected_channels=["RPM", "SPEED"]),
            _anomaly_event(affected_channels=["LTFT_B1"]),
        ],
    )
    out = search_anomaly_log(
        kind="anomalies",
        anomalies_dir=tmp_path,
        audit_path=tmp_path / "audit.jsonl",
        affected_channel="LTFT_B1",
    )
    assert out["count"] == 1
    assert "LTFT_B1" in out["events"][0]["affected_channels"]


def test_anomalies_search_uses_filename_date_to_skip_files(tmp_path: Path) -> None:
    """Files outside the [since, until] window are pruned by filename alone."""
    april = _anomaly_event(timestamp="2026-04-01T05:00:00+00:00")
    may = _anomaly_event(timestamp="2026-05-18T05:00:00+00:00")
    _write_anomalies(tmp_path, "2026-04-01", [april])
    _write_anomalies(tmp_path, "2026-05-18", [may])
    out = search_anomaly_log(
        kind="anomalies",
        anomalies_dir=tmp_path,
        audit_path=tmp_path / "audit.jsonl",
        since="2026-05-01T00:00:00Z",
        until="2026-05-31T23:59:59Z",
    )
    assert out["count"] == 1
    assert out["events"][0]["timestamp"].startswith("2026-05-18")


def test_anomalies_search_contains_matches_raw_line(tmp_path: Path) -> None:
    """Free-text ``contains`` finds substrings the structured filters can't."""
    today = datetime.now(UTC).date().isoformat()
    _write_anomalies(
        tmp_path,
        today,
        [
            _anomaly_event(detail={"note": "snow on the road"}),
            _anomaly_event(detail={"note": "fuel cap loose"}),
        ],
    )
    out = search_anomaly_log(
        kind="anomalies",
        anomalies_dir=tmp_path,
        audit_path=tmp_path / "audit.jsonl",
        contains="fuel cap",
    )
    assert out["count"] == 1
    assert out["events"][0]["detail"]["note"] == "fuel cap loose"


def test_anomalies_search_respects_limit(tmp_path: Path) -> None:
    today = datetime.now(UTC).date().isoformat()
    _write_anomalies(
        tmp_path,
        today,
        [
            _anomaly_event(timestamp=f"{today}T{hour:02d}:00:00+00:00", score=float(hour))
            for hour in range(10)
        ],
    )
    out = search_anomaly_log(
        kind="anomalies",
        anomalies_dir=tmp_path,
        audit_path=tmp_path / "audit.jsonl",
        limit=3,
    )
    assert out["count"] == 3
    # Newest-first → highest hour first.
    assert [e["score"] for e in out["events"]] == [9.0, 8.0, 7.0]


def test_anomalies_search_requires_anomalies_dir(tmp_path: Path) -> None:
    """Without ``anomalies_dir`` configured the tool returns a structured error."""
    out = search_anomaly_log(
        kind="anomalies",
        anomalies_dir=None,
        audit_path=tmp_path / "audit.jsonl",
    )
    assert out["ok"] is False
    assert "CANARY_ANOMALIES_DIR" in out["error"]


def test_anomalies_search_rejects_bad_timestamp(tmp_path: Path) -> None:
    out = search_anomaly_log(
        kind="anomalies",
        anomalies_dir=tmp_path,
        audit_path=tmp_path / "audit.jsonl",
        since="not-a-date",
    )
    assert out["ok"] is False
    assert "invalid" in out["error"].lower()


# ---------------------------------------------------------------------------
# Audit search
# ---------------------------------------------------------------------------


def test_audit_search_filters_by_action_and_actor(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    _write_audit(
        audit_path,
        [
            {
                "timestamp": "2026-05-18T10:00:00+00:00",
                "action": "mcp.start_capture",
                "result": "ok",
                "payload": {"actor": "mcp"},
            },
            {
                "timestamp": "2026-05-18T11:00:00+00:00",
                "action": "capture.start",
                "result": "ok",
                "payload": {},
            },
        ],
    )
    out = search_anomaly_log(
        kind="audit",
        anomalies_dir=None,
        audit_path=audit_path,
        action="mcp.start_capture",
        actor="mcp",
    )
    assert out["count"] == 1
    assert out["events"][0]["action"] == "mcp.start_capture"


def test_audit_search_filters_by_result(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    _write_audit(
        audit_path,
        [
            {
                "timestamp": "2026-05-18T10:00:00+00:00",
                "action": "mcp.stop_capture",
                "result": "error",
                "payload": {"actor": "mcp", "error": "no capture"},
            },
            {
                "timestamp": "2026-05-18T11:00:00+00:00",
                "action": "mcp.stop_capture",
                "result": "ok",
                "payload": {"actor": "mcp"},
            },
        ],
    )
    out = search_anomaly_log(
        kind="audit",
        anomalies_dir=None,
        audit_path=audit_path,
        result="error",
    )
    assert out["count"] == 1
    assert "error" in out["events"][0]["payload"]


def test_audit_search_skips_malformed_lines_unless_contains_used(tmp_path: Path) -> None:
    """Bad lines are dropped silently in structured filters; ``contains`` surfaces them."""
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-18T10:00:00+00:00",
                "action": "mcp.x",
                "result": "ok",
                "payload": {"actor": "mcp"},
            }
        )
        + "\nnot-json\n"
    )
    structured = search_anomaly_log(
        kind="audit", anomalies_dir=None, audit_path=audit_path, action="mcp.x"
    )
    assert structured["count"] == 1
    surfaced = search_anomaly_log(
        kind="audit", anomalies_dir=None, audit_path=audit_path, contains="not-json"
    )
    assert any(r.get("raw") == "not-json" for r in surfaced["events"])


def test_audit_search_unbounded_when_no_since(tmp_path: Path) -> None:
    """Audit search has no default lookback — old entries are returned."""
    audit_path = tmp_path / "audit.jsonl"
    long_ago = (datetime.now(UTC) - timedelta(days=365)).isoformat()
    _write_audit(
        audit_path,
        [
            {"timestamp": long_ago, "action": "mcp.x", "result": "ok", "payload": {}},
        ],
    )
    out = search_anomaly_log(kind="audit", anomalies_dir=None, audit_path=audit_path)
    assert out["count"] == 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_kind", ["captures", "", "ANOMALIES "])
def test_invalid_kind_rejected(tmp_path: Path, bad_kind: str) -> None:
    out = search_anomaly_log(
        kind=bad_kind, anomalies_dir=tmp_path, audit_path=tmp_path / "audit.jsonl"
    )
    assert out["ok"] is False
    assert "kind" in out["error"]

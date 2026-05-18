"""Tests for the in-process analysis tools."""

from __future__ import annotations

import json
from pathlib import Path

from src.mcp_server.tools.analysis import signal_lookup


def _write_catalog(path: Path) -> None:
    """Write a minimal FSM signal catalog covering ECM + TCM_5AT.

    Mirrors the shape :func:`src.analysis.signal_lookup.load_catalog` reads.
    """
    catalog = {
        "modules": [
            {
                "module": "ECM",
                "params": [
                    {
                        "name": "MASS AIRFLOW",
                        "page": 12,
                        "expected_ranges": [{"min": 0.0, "max": 200.0, "unit": "g/s"}],
                    },
                    {"name": "ENGINE COOLANT TEMP", "page": 13, "expected_ranges": []},
                ],
            },
            {
                "module": "TCM_5AT",
                "params": [{"name": "ATF TEMP", "page": 5, "expected_ranges": []}],
            },
        ]
    }
    path.write_text(json.dumps(catalog))


def test_signal_lookup_finds_match_across_modules(tmp_path: Path) -> None:
    """Without a CAN ID, search spans every module's params."""
    catalog = tmp_path / "signals.json"
    _write_catalog(catalog)
    result = signal_lookup(catalog, tag_label="ecm_maf_livedata")
    assert result["ok"] is True
    names = [m["name"] for m in result["matches"]]
    assert "MASS AIRFLOW" in names


def test_signal_lookup_narrows_by_can_id(tmp_path: Path) -> None:
    """``can_id=0x7E0`` restricts candidates to ECM, so TCM params drop out."""
    catalog = tmp_path / "signals.json"
    _write_catalog(catalog)
    result = signal_lookup(catalog, tag_label="atf_temp", can_id=0x7E0)
    assert result["module"] == "ECM"
    assert all(m["module"] == "ECM" for m in result["matches"])


def test_signal_lookup_returns_error_payload_when_catalog_missing(tmp_path: Path) -> None:
    """A missing catalog file is a structured failure, not an exception."""
    result = signal_lookup(tmp_path / "no.json", tag_label="anything")
    assert result["ok"] is False
    assert "not found" in result["error"]
    assert result["matches"] == []

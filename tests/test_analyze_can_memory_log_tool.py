from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.analyze_can_memory_log import analyze_capture, load_capture


def _write_capture(path: Path, *, samples: int = 90) -> None:
    rows: list[dict[str, object]] = [
        {
            "type": "header",
            "address": "0xFFFF1000",
            "length": 3,
        }
    ]
    for index in range(samples):
        acquired = 1_700_000_000.0 + index * 0.05
        value = 100 + (index % 30) * 7
        rows.extend(
            [
                {
                    "type": "read",
                    "index": index,
                    "t_send": acquired,
                    "t_recv": acquired + 0.001,
                    "payload_hex": f"55{value:04x}",
                },
                {
                    "type": "did",
                    "memory_index": index,
                    "did": "0x1206",
                    "t_send": acquired + 0.002,
                    "t_recv": acquired + 0.003,
                    "payload_hex": f"{value:04x}",
                },
            ]
        )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_load_and_compare_uses_paired_observations(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    _write_capture(path)

    capture = load_capture(path)
    results, _ = analyze_capture(capture)

    exact = next(row for row in results if row["a"] == "uprev:MEM_FFFF1001_U16BE")
    assert exact["b"] == "uprev:DID_1206_RAW"
    assert exact["n_pairs"] == 90
    assert exact["r_squared"] == pytest.approx(1.0)
    assert exact["slope"] == pytest.approx(1.0)
    assert exact["offset"] == pytest.approx(0.0)
    assert exact["scale_verdict"] == "stable"


def test_rejects_memory_payload_length_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "header", "address": "0x1000", "length": 2}),
                json.dumps(
                    {
                        "type": "read",
                        "index": 0,
                        "t_send": 1.0,
                        "t_recv": 1.1,
                        "payload_hex": "01",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="header says 2"):
        load_capture(path)

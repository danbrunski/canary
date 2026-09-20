from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from tools.analyze_uprev_kline_capture import analyze, load_reference_rows, load_uprev_samples


def test_load_uprev_samples_expands_dynamic_reply(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"kind": "bootstrap", "timestamp": "2026-09-20T18:00:00Z"}),
                json.dumps(
                    {
                        "kind": "stream_sample",
                        "timestamp": "2026-09-20T18:00:01.123Z",
                        "values": {
                            "0x1101": {"raw": "8d", "raw_int": 141, "width": 1},
                            "0x1201": {"raw": "00d8", "raw_int": 216, "width": 2},
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    frame = load_uprev_samples(path)

    assert list(frame["channel"]) == ["DID_1101", "DID_1201"]
    assert list(frame["value_number"]) == [141.0, 216.0]
    assert list(frame["value_text"]) == ["8d", "00d8"]
    assert set(frame["source"]) == {"uprev"}
    assert isinstance(frame["timestamp"].dtype, pd.DatetimeTZDtype)
    assert str(frame["timestamp"].dt.tz) == "UTC"


def test_load_uprev_samples_rejects_missing_raw_value(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    path.write_text(
        json.dumps(
            {
                "kind": "stream_sample",
                "timestamp": "2026-09-20T18:00:01Z",
                "values": {"0x1101": {"raw": "8d"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no numeric raw_int"):
        load_uprev_samples(path)


def test_load_uprev_samples_decodes_mixed_did_and_memory_reply(tmp_path: Path) -> None:
    path = tmp_path / "mixed.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-09-20T20:00:00+00:00",
                "kind": "mixed_stream_sample",
                "dids": {
                    "0x1201": {"raw": "00d8", "raw_int": 216, "width": 2},
                },
                "memory": {
                    "0xffff26be": {"raw": "4a", "raw_int": 74, "width": 1},
                    "0xffff26bf": {"raw": "80", "raw_int": 128, "width": 1},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    frame = load_uprev_samples(path).set_index("channel")

    assert frame.loc["DID_1201", "value_number"] == 216
    assert frame.loc["MEM_FFFF26BE", "value_number"] == 74
    assert frame.loc["MEM_FFFF26BF", "value_number"] == 128
    assert frame.loc["MEM_BE16_FFFF26BE", "value_number"] == 0x4A80
    assert frame.loc["MEM_BE16_FFFF26BE", "value_text"] == "4a80"


def test_load_reference_rows_filters_time_and_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-09-20T18:00:00Z",
                    "2026-09-20T18:00:01Z",
                    "2026-09-20T18:00:01Z",
                    "2026-09-20T18:00:01Z",
                ],
                utc=True,
            ),
            "source": ["decoded", "decoded", "gps", "obd"],
            "channel": ["ECM_RPM", "OTHER", "speed", "SHORT_FUEL_TRIM_1"],
        }
    )
    monkeypatch.setattr("tools.analyze_uprev_kline_capture.read_session", lambda _: frame)

    result = load_reference_rows(
        tmp_path,
        (
            "decoded:ECM_RPM",
            "gps:speed",
            "obd:SHORT_FUEL_TRIM_1",
            "uprev:DID_1201",
        ),
        pd.Timestamp("2026-09-20T17:59:59Z"),
        pd.Timestamp("2026-09-20T18:00:02Z"),
    )

    assert list(zip(result["source"], result["channel"], strict=True)) == [
        ("decoded", "ECM_RPM"),
        ("gps", "speed"),
        ("obd", "SHORT_FUEL_TRIM_1"),
    ]


def test_analyze_skips_trivial_self_comparison(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "kind": "stream_sample",
                    "timestamp": f"2026-09-20T18:00:{index:02d}Z",
                    "values": {
                        "0x1201": {
                            "raw": index.to_bytes(2, "big").hex(),
                            "raw_int": index,
                            "width": 2,
                        }
                    },
                }
            )
            for index in range(31)
        ),
        encoding="utf-8",
    )

    results, refusals = analyze(
        path,
        tmp_path,
        ("uprev:DID_1201",),
        tolerance="1ms",
        min_pairs=30,
    )

    assert results == []
    assert refusals == []

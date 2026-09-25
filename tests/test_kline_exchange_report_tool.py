"""K01.2: one command writes a sniffed session's K-line report as Markdown and JSON."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.collection.kline.profile import load_kline_profile
from src.collection.kline.sniff import FrameDecoder
from src.collection.kline_collector import SniffRecorder
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.builders import abs_scan_tool_frames, kline_wire
from tools import kline_exchange_report

PROFILE = Path("data/nissan-xterra-2008/kline.yaml")


def _write_session(root: Path) -> Path:
    stream = kline_wire(abs_scan_tool_frames(cycles=4, brake_cycles=(1,)))
    decoder, recorder = FrameDecoder(), SniffRecorder(load_kline_profile(PROFILE), {})
    records: list[TelemetryRecord] = []
    last = stream[0][0]
    for at, byte in stream:
        for frame in decoder.feed(at, byte, quiet_s=at - last):
            records += recorder.observe(frame, 1_790_000_000.0 + frame.finished)
        last = at
    session = root / "2026-09-24T162507"
    write_records_to_parquet(records, session)
    return session


def test_the_report_command_writes_markdown_and_json(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    out = tmp_path / "analyses"
    code = kline_exchange_report.main(
        [
            str(session),
            "--kline-profile",
            str(PROFILE),
            "--out-dir",
            str(out),
            "--bits",
            "ABS:1303",
            "--pair",
            "ABS:1202=decoded:ECM_RPM",
        ]
    )
    assert code == 0
    text = (out / "2026-09-24T162507-kline-exchanges.md").read_text()
    assert "## Module ABS (0x28)" in text
    assert "## Bit transitions: ABS:1303" in text and "| 0x0200 | 1 |" in text
    # No reference was recorded: the pairing says so rather than inventing a ratio.
    assert "## Pairing: kline:ABS:1202 vs decoded:ECM_RPM" in text and "refused:" in text
    payload = json.loads((out / "2026-09-24T162507-kline-exchanges.json").read_text())
    assert {m["module"] for m in payload["modules"]} == {"ABS", "ECM", "unknown"}
    assert [b["mask"] for b in payload["bits"]["ABS:1303"]] == [0x0200, 0x0200]
    assert "refused" in payload["pairs"][0]


def test_a_session_without_kline_is_refused_with_its_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = tmp_path / "2026-09-24T170000"
    write_records_to_parquet([TelemetryRecord.obd(1_790_000_000.0, "RPM", 700.0, "rpm")], session)
    out = tmp_path / "analyses"
    code = kline_exchange_report.main(
        [str(session), "--kline-profile", str(PROFILE), "--out-dir", str(out)]
    )
    assert code == 2
    assert "collector was not running" in capsys.readouterr().err
    assert not out.exists()

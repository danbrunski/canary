"""K01.1-K01.3: a sniffed K-line session, re-read by module.

Two inputs. UpRev's real ECM session (``tests/fixtures/kline``, 2026-09-23,
the sniffer's own test fixture) run through the collector's real recording
path; and a scan tool on the ABS built from the bytes documented for
2026-09-24 (``tests.builders.abs_scan_tool_frames``: modelled, not the capture
itself, which is still on the Pi).
"""

from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from src.analysis.channels import ChannelSpecError, read_channels
from src.analysis.compare import ComparisonRefusedError
from src.analysis.kline_exchanges import (
    MIN_CYCLE_READS,
    KlineReportError,
    attribute_modules,
    bit_transitions,
    exchange_report,
    pair_value,
    render_markdown,
    transitions_between,
    wire_frames,
)
from src.collection.kline.profile import load_kline_profile
from src.collection.kline.sniff import FrameDecoder, SniffedBreak
from src.collection.kline.transport import RxChunk, WakePulse
from src.collection.kline_collector import SniffRecorder, break_record, wake_record
from src.preprocessing.loader import read_session
from src.signals.pairing import PairingPolicy
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.builders import ABS_SCREEN_CYCLE, abs_scan_tool_frames, kline_wire

PROFILE_PATH = Path("data/nissan-xterra-2008/kline.yaml")
PROFILE = load_kline_profile(PROFILE_PATH)
UPREV = Path(__file__).parent / "fixtures" / "kline" / "uprev_session_bytes.json.gz"
EPOCH = datetime(2026, 9, 24, 16, 32, tzinfo=UTC).timestamp()


def _uprev_stream() -> list[tuple[float, int]]:
    fixture = json.loads(gzip.open(UPREV, "rt").read())
    t, out = 0.0, []
    for byte, dt in zip(bytes.fromhex(fixture["bytes"]), fixture["dt_us"], strict=True):
        t += dt / 1e6
        out.append((t, byte))
    return out


def _records(stream: list[tuple[float, int]], *, legacy: bool = False) -> list[TelemetryRecord]:
    """The rows the sniffer stores, through its own recording path. ``legacy``
    rewrites them as a pre-K01.1 sniffer stored them: ``DID_<hex>`` values,
    frames with no module and no timing uncertainty."""
    decoder, recorder = FrameDecoder(), SniffRecorder(PROFILE, {})
    records: list[TelemetryRecord] = []
    last = stream[0][0]
    for at, byte in stream:
        for frame in decoder.feed(at, byte, quiet_s=at - last):
            records += recorder.observe(frame, EPOCH + frame.finished - stream[0][0])
        last = at
    if not legacy:
        return records
    out = []
    for r in records:
        meta = {
            k: v
            for k, v in r.metadata.items()
            if k not in ("module", "module_address", "timing_uncertainty_s")
        }
        channel = r.channel if r.channel == "FRAME" else "DID_" + r.channel.split(":")[1]
        out.append(replace(r, channel=channel, metadata=meta))
    return out


def _session(tmp_path: Path, records: list[TelemetryRecord], name: str) -> pd.DataFrame:
    out = tmp_path / name
    write_records_to_parquet(records, out)
    return read_session(out)


@pytest.fixture
def abs_session(tmp_path: Path) -> pd.DataFrame:
    stream = kline_wire(abs_scan_tool_frames(cycles=6, brake_cycles=(2, 3)))
    return _session(tmp_path, _records(stream), "2026-09-24T162507")


# --------------------------------------------------------------------------- K01.1


def test_frames_carry_their_recorded_module(abs_session: pd.DataFrame) -> None:
    frames = wire_frames(abs_session)
    assert {wf.module_basis for wf in frames} == {"recorded", "unknown"}
    assert [wf.module for wf in frames[:2]] == [None, None]  # attached mid-session
    assert {wf.module for wf in frames[2:]} == {0x28, 0x10}
    assert all(wf.timing_uncertainty_s is not None for wf in frames)


def test_a_legacy_session_gets_its_modules_back_from_its_own_frames(tmp_path: Path) -> None:
    stream = kline_wire(abs_scan_tool_frames(cycles=3))
    legacy = _session(tmp_path, _records(stream, legacy=True), "2026-09-24T162507")
    # The collision K01.1 exists for: two modules' 0x1210 in one legacy row name.
    assert set(legacy.loc[legacy["channel"] == "DID_1210", "value_text"]) == {"ffff", "001a"}
    frames = wire_frames(legacy)
    assert {wf.module_basis for wf in frames} == {"reconstructed", "unknown"}
    attributed = attribute_modules(legacy, PROFILE)
    values = set(zip(attributed["channel"], attributed["value_text"], strict=True))
    assert {("ABS:1210", "ffff"), ("ECM:1210", "001a"), ("unknown:1202", "00f5")} <= values
    assert not attributed["channel"].str.startswith("DID_").any()
    assert (legacy["channel"] == "DID_1210").sum() == 4  # the input is not rewritten
    assert set(attributed.loc[attributed["channel"] == "ABS:1210", "source_channel"]) == {
        "kline:ABS:1210"
    }


def test_a_legacy_kline_did_is_refused_by_the_channel_reader(tmp_path: Path) -> None:
    session = tmp_path / "2026-09-24T162507"
    write_records_to_parquet(
        _records(kline_wire(abs_scan_tool_frames(cycles=1)), legacy=True), session
    )
    with pytest.raises(ChannelSpecError, match="attribute_modules"):
        read_channels(session, ["kline:DID_1210"])
    # ... and the ECM catalog's did: reader does not take the ABS's 0x1210 for
    # TPS2; it says which kind of empty that is.
    with pytest.raises(ChannelSpecError, match="pre-K01.1 K-line rows.*attribute_modules"):
        read_channels(session, ["did:0x1210"])
    # A DID never read at all is still plainly empty.
    assert read_channels(session, ["did:0x1201"]).empty


# --------------------------------------------------------------------------- K01.2


def test_the_abs_screen_cycle_is_separated_from_one_off_reads(abs_session: pd.DataFrame) -> None:
    report = exchange_report(abs_session, PROFILE, session="2026-09-24T162507")
    modules = {m.module: m for m in report.modules}
    assert set(modules) == {"ABS", "ECM", "unknown"}
    abs_ = modules["ABS"]
    assert abs_.address == "0x28"
    assert abs_.cycle == [f"{did:04X}" for did, _ in ABS_SCREEN_CYCLE]
    assert abs_.cycle_repetitions >= 4
    assert abs_.one_off == ["1100", "1200", "1307"]
    by_did = {d.did: d for d in abs_.dids}
    assert all(by_did[f"{did:04X}"].reads == 6 for did, _ in ABS_SCREEN_CYCLE)
    assert MIN_CYCLE_READS <= 6
    # A rate is the median interval between one DID's reads, never a count
    # over a span; the cycle is 11 reads x 2 frames + TesterPresent.
    assert by_did["1202"].median_interval_s == pytest.approx(24 * 0.03 + 0.001 * 158, rel=0.05)
    assert by_did["1202"].binding == "ENGINE_SPEED (confirmed)"
    assert "STOP_LAMP_SWITCH=0x0200 (confirmed)" in (by_did["1303"].binding or "")
    assert by_did["1303"].width == 2 and by_did["1303"].values == {"a0fe": 4, "a2fe": 2}
    assert by_did["1307"].negative == {"0x12 subFunctionNotSupported-invalidFormat": 1}


def test_nrcs_services_and_orphans_are_named(abs_session: pd.DataFrame) -> None:
    report = exchange_report(abs_session, PROFILE)
    abs_ = next(m for m in report.modules if m.module == "ABS")
    assert abs_.services["ReadDataByCommonIdentifier"] == 6 * len(ABS_SCREEN_CYCLE) + 3
    assert abs_.services["TesterPresent"] == 6
    assert abs_.services["ReadEcuIdentification"] == 2
    assert any("ReadEcuIdentification 00: 0x12" in nrc for nrc in abs_.nrcs)
    others = {o["request"]: o for o in abs_.other_requests}
    assert others["3e"]["reply"] == "7e" and others["3e"]["count"] == 6
    assert report.orphan_replies == 1  # 62 11 18 A1: its request was never heard
    assert report.direction_corrections == 0
    assert report.timing.recorded == report.frames


def test_the_report_renders_markdown_and_serialises(abs_session: pd.DataFrame) -> None:
    report = exchange_report(abs_session, PROFILE, session="2026-09-24T162507")
    text = render_markdown(report)
    assert "## Module ABS (0x28)" in text
    assert "Screen cycle (11 DIDs" in text
    assert "| 0x1307 | one_off | 1 |" in text
    json.dumps(report.as_dict())


def test_uprevs_real_session_reports_as_the_ecms(tmp_path: Path) -> None:
    """The sniffer's real 2026-09-23 fixture: twelve StartCommunications to
    the ECM, 723 answered length-frame requests, UpRev's AC/21 logging."""
    frame = _session(tmp_path, _records(_uprev_stream()), "2026-09-23T220000")
    report = exchange_report(frame, PROFILE)
    (ecm,) = report.modules
    assert (ecm.module, ecm.address) == ("ECM", "0x10")
    assert ecm.frames == 1462
    assert ecm.services["StartCommunication"] == 12
    assert ecm.services["ReadDataByLocalIdentifier"] == 590
    assert ecm.services["DynamicallyDefineLocalIdentifier"] >= 3
    assert {d.did for d in ecm.dids} == {"1103"}  # UpRev's ECU check
    assert report.orphan_replies == 0


def test_a_session_without_kline_says_which_kind_of_empty(tmp_path: Path) -> None:
    rpm = TelemetryRecord.obd(EPOCH, "RPM", 700.0, "rpm")
    with pytest.raises(KlineReportError, match="collector was not running"):
        exchange_report(_session(tmp_path, [rpm], "a"), PROFILE)
    value = TelemetryRecord.from_epoch(EPOCH, source="kline", channel="ECM:1201", value="02c8")
    with pytest.raises(KlineReportError, match="no FRAME or BREAK rows"):
        exchange_report(_session(tmp_path, [value], "b"), PROFILE)


# --------------------------------------------------------------------------- K01.3


def test_each_bit_transition_is_listed_with_its_time(abs_session: pd.DataFrame) -> None:
    transitions = bit_transitions(abs_session, "ABS:1303")
    assert [(t.mask, t.became) for t in transitions] == [(0x0200, True), (0x0200, False)]
    assert (transitions[0].word_before, transitions[0].word_after) == (0xA0FE, 0xA2FE)
    window = transitions_between(transitions, transitions[0].at, transitions[1].at)
    assert list(window) == [0x0200] and len(window[0x0200]) == 2
    with pytest.raises(KlineReportError, match="never read"):
        bit_transitions(abs_session, "ABS:1399")


def _pairing_session(tmp_path: Path, raws: list[int]) -> pd.DataFrame:
    """ABS 0x1202 counts beside the ECM's CAN rpm (the 0x1F9 frame, whose
    ``decoded:ECM_RPM`` is raw, 8 counts/rpm, decoded on read: P02.8), 20 ms
    apart, at 3.125 rpm per ABS count."""
    records: list[TelemetryRecord] = []
    for k, raw in enumerate(raws):
        t = EPOCH + k * 0.5
        records.append(
            TelemetryRecord.from_epoch(
                t, source="kline", channel="ABS:1202", value=raw.to_bytes(2, "big").hex()
            )
        )
        counts = round(raw * 3.125 * 8)
        payload = bytes([0x20, 0x00, (counts >> 8) & 0xFF, counts & 0xFF, 0, 0, 0, 0])
        records.append(TelemetryRecord.can(t + 0.02, 0x1F9, payload))
    return _session(tmp_path, records, "2026-09-24T164000")


def test_a_value_is_paired_by_same_instant_ratio(tmp_path: Path) -> None:
    frame = _pairing_session(tmp_path, [232 + (k * 7) % 103 for k in range(24)])
    pairing = pair_value(
        frame, "kline:ABS:1202", "decoded:ECM_RPM", policy=PairingPolicy.within("100ms")
    )
    assert pairing.n_pairs == 24
    assert pairing.ratio_median == pytest.approx(3.125, rel=1e-3)
    assert pairing.ratio_spread_pct < 0.1
    assert "per-sample ratio median 3.125" in pairing.describe()


def test_pairing_refuses_rather_than_matching_aggregates(tmp_path: Path) -> None:
    frame = _pairing_session(tmp_path, [240] * 24)  # parked: nothing moves
    with pytest.raises(ComparisonRefusedError):
        pair_value(frame, "kline:ABS:1202", "decoded:ECM_RPM", policy=PairingPolicy.within("100ms"))
    too_far = _pairing_session(tmp_path / "far", [232 + k for k in range(24)])
    with pytest.raises(ComparisonRefusedError, match="aligned pairs"):
        pair_value(too_far, "kline:ABS:1202", "decoded:ECM_RPM", policy=PairingPolicy.within("5ms"))


# --------------------------------------------------------------------------- wake-ups (H05.9)

SRS_START = bytes.fromhex("81 58 fc 81 56")
SRS_START_REPLY = bytes.fromhex("83 fc 58 c1 6d 8f 94")


def _wake_session(tmp_path: Path, *, canary_answered: bool | None = None) -> pd.DataFrame:
    """Known answer, through the sniffer's own recording path: ``FF 00 00`` at
    t0, then ``81 58 FC 81 56`` at t0 + 50 ms, then the SRS's reply. With
    ``canary_answered``, CANary's own 25/25 wake-up of the SRS a second later."""
    decoder, recorder = FrameDecoder(), SniffRecorder(PROFILE, {})
    t0 = 500.0
    to_epoch = EPOCH - t0
    chunks = [
        (t0, b"\xff\x00\x00", 0.5),
        (t0 + 0.050, SRS_START, 0.049),
        (t0 + 0.080, SRS_START_REPLY, 0.029),
    ]
    records: list[TelemetryRecord] = []
    for at, data, quiet in chunks:
        chunk = RxChunk(at=at, data=data, quiet_s=quiet, after=at - quiet, char_s=0.0, marked=True)
        for event in decoder.feed_chunk_events(chunk):
            if isinstance(event, SniffedBreak):
                records.append(break_record(event, to_epoch))
            else:
                records += recorder.observe(event, event.finished + to_epoch)
    if canary_answered is not None:
        pulse = WakePulse(
            name="iso14230-fast",
            wanted_low_ms=25.0,
            wanted_high_ms=25.0,
            fall=t0 + 1.0,
            release=t0 + 1.025,
            end=t0 + 1.050,
            readback=b"\x00",
            char_s=10 / 10400,
            target=0x58,
            answered=canary_answered,
        )
        records.append(wake_record(pulse, to_epoch, PROFILE.module_label(0x58)))
    return _session(tmp_path, records, "2026-10-08T120000")


def test_one_break_before_the_srs_start_spans_fifty_ms(tmp_path: Path) -> None:
    report = exchange_report(_wake_session(tmp_path), PROFILE)
    (wake,) = report.wake_ups
    assert wake.origin == "foreign"
    assert wake.target == "SRS"
    assert wake.start_communication == "81 58 fc 81 56"
    assert wake.answered is True
    assert wake.span_ms == pytest.approx(50.0, abs=0.01)
    # The UART never reports the release: the low is only bounded.
    assert wake.low_ms is None and wake.high_ms is None
    assert wake.low_ms_max is not None and wake.span_ms is not None
    assert wake.low_ms_max >= wake.span_ms
    assert report.starts_without_break == 0


def test_canarys_wake_up_sits_beside_the_testers(tmp_path: Path) -> None:
    report = exchange_report(_wake_session(tmp_path, canary_answered=False), PROFILE)
    tester, canary = report.wake_ups
    assert (tester.origin, canary.origin) == ("foreign", "canary")
    assert canary.low_ms == pytest.approx(25.0) and canary.high_ms == pytest.approx(25.0)
    assert (canary.target, canary.answered, canary.wakeup) == ("SRS", False, "iso14230-fast")
    markdown = render_markdown(report)
    assert "## Wake-ups" in markdown
    assert "| canary | iso14230-fast | 25.0 | 25.0 | 50.0" in markdown
    assert "| foreign | — |" in markdown
    json.dumps(report.as_dict(), default=str)


def test_a_session_before_break_recording_says_its_wake_ups_were_not_measured(
    tmp_path: Path,
) -> None:
    stream = kline_wire(abs_scan_tool_frames(cycles=2, brake_cycles=()))
    report = exchange_report(_session(tmp_path, _records(stream), "x"), PROFILE)
    assert report.wake_ups == [] and report.starts_without_break >= 1
    assert any("predates break recording" in note for note in report.notes)


def test_only_canarys_wake_ups_still_report(tmp_path: Path) -> None:
    pulse = WakePulse(
        "long-low", 518.9, 18.1, 10.0, 10.519, 10.537, b"\x00", 10 / 10400, 0x58, False
    )
    record = wake_record(pulse, EPOCH - 10.0, "SRS")
    report = exchange_report(_session(tmp_path, [record], "y"), PROFILE)
    (wake,) = report.wake_ups
    assert wake.origin == "canary" and wake.answered is False
    assert wake.low_ms == pytest.approx(519.0) and report.modules == []

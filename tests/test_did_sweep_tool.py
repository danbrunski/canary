"""Tests for tools/did_sweep.py — the did_sweep job's actual worker script."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import can
import pytest

from src.analysis.did_scan import DidSnapshot
from src.decode.dids import DidAccess
from tests.configs import write_isolated_config
from tools import _bus_tool, did_sweep


def _write_config(tmp_path: Path, channel: str) -> Path:
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    return write_isolated_config(
        tmp_path, "daemon", storage=storage, hardware={"can": {"channel": channel}}
    )


# ---------------------------------------------------------------------------
# resolve_session (shared with every bus tool since R06.1)
# ---------------------------------------------------------------------------


def test_resolve_session_none_disables_gating() -> None:
    assert _bus_tool.resolve_session("none") is None


def test_resolve_session_explicit_hex() -> None:
    assert _bus_tool.resolve_session("C0") == 0xC0


def test_resolve_session_auto_delegates_to_did_access(monkeypatch: pytest.MonkeyPatch) -> None:
    access = DidAccess(ecu_request_id=0x7E0, ecu_response_id=0x7E8, session=0xC0)
    monkeypatch.setattr(_bus_tool, "did_access", lambda: access)
    assert _bus_tool.resolve_session("auto") == 0xC0


def test_resolve_session_auto_with_no_access_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_bus_tool, "did_access", lambda: None)
    assert _bus_tool.resolve_session("auto") is None


# ---------------------------------------------------------------------------
# main() — range/header validation, no bus needed
# ---------------------------------------------------------------------------


def test_main_rejects_inverted_range(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, "did-sweep-unused")
    rc = did_sweep.main(
        [
            "--config",
            str(cfg_path),
            "--start",
            "0x1200",
            "--end",
            "0x1100",
            "--label",
            "x",
            "--out",
            str(tmp_path / "out.json"),
        ]
    )
    assert rc == 1


def test_main_rejects_non_hex_header(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, "did-sweep-unused2")
    rc = did_sweep.main(
        [
            "--config",
            str(cfg_path),
            "--start",
            "0x1100",
            "--end",
            "0x1101",
            "--label",
            "x",
            "--header",
            "ZZZ",
            "--out",
            str(tmp_path / "out.json"),
        ]
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# main() — real virtual-bus sweep, no session gating
# ---------------------------------------------------------------------------


def _did_reply(*, did: int, responder_id: int, data: bytes) -> can.Message:
    payload = bytes([0x62, (did >> 8) & 0xFF, did & 0xFF]) + data
    frame = bytes([len(payload)]) + payload
    frame += bytes(8 - len(frame))
    return can.Message(arbitration_id=responder_id, data=frame, is_extended_id=False)


def _negative_reply(*, service: int, nrc: int, responder_id: int) -> can.Message:
    frame = bytes([0x03, 0x7F, service, nrc]) + bytes(4)
    return can.Message(arbitration_id=responder_id, data=frame, is_extended_id=False)


def _batched_reply(*, dids: list[int], values: dict[int, bytes], responder_id: int) -> can.Message:
    body = bytes([0x62])
    for did in dids:
        body += bytes([(did >> 8) & 0xFF, did & 0xFF]) + values[did]
    frame = bytes([len(body)]) + body
    frame += bytes(8 - len(frame))
    return can.Message(arbitration_id=responder_id, data=frame, is_extended_id=False)


def _requested_dids(data: bytes) -> list[int]:
    """The DIDs in a single-frame ``<len> 22 <did…>`` request."""
    count = (data[0] - 1) // 2
    return [(data[2 + 2 * i] << 8) | data[3 + 2 * i] for i in range(count)]


def _run_fake_ecu(
    channel: str,
    values: dict[int, bytes],
    *,
    stop: threading.Event,
    ready: threading.Event | None = None,
    reject_batches: bool = False,
) -> None:
    """A Mode 22 responder. ``reject_batches`` models the 2008 Nissan ECM,
    which answers ``7F 22 12`` to any multi-DID read while answering each of
    those DIDs on its own (measured on the truck 2026-09-17)."""
    ecu = can.interface.Bus(interface="virtual", channel=channel)
    if ready is not None:
        # The virtual bus only receives what is sent after it exists. Without
        # this handshake the sweep's first request can go out before the ECU
        # is listening and simply vanish — rare when the machine is idle,
        # routine under `pytest -n auto`.
        ready.set()
    try:
        while not stop.is_set():
            frame = ecu.recv(timeout=0.1)
            if frame is None:
                continue
            data = bytes(frame.data)
            if data[1] != 0x22:
                continue
            dids = _requested_dids(data)
            if len(dids) > 1:
                if reject_batches:
                    ecu.send(_negative_reply(service=0x22, nrc=0x12, responder_id=0x7E8))
                    continue
                if all(did in values for did in dids):
                    ecu.send(_batched_reply(dids=dids, values=values, responder_id=0x7E8))
                else:
                    ecu.send(_negative_reply(service=0x22, nrc=0x31, responder_id=0x7E8))
                continue
            value = values.get(dids[0])
            if value is not None:
                ecu.send(_did_reply(did=dids[0], responder_id=0x7E8, data=value))
    finally:
        ecu.shutdown()


def test_main_sweeps_and_writes_a_snapshot(tmp_path: Path) -> None:
    channel = "did-sweep-e2e"
    cfg_path = _write_config(tmp_path, channel)
    out_path = tmp_path / "snap.json"
    values = {0x1100: bytes([0x0A, 0x0B]), 0x1102: bytes([0xFF])}
    stop = threading.Event()
    ready = threading.Event()
    t = threading.Thread(
        target=_run_fake_ecu,
        args=(channel, values),
        kwargs={"stop": stop, "ready": ready},
        daemon=True,
    )
    t.start()
    assert ready.wait(timeout=5.0), "fake ECU never came up"
    try:
        rc = did_sweep.main(
            [
                "--config",
                str(cfg_path),
                "--start",
                "0x1100",
                "--end",
                "0x1102",
                "--label",
                "control",
                "--session",
                "none",
                "--read-timeout",
                "0.2",
                "--out",
                str(out_path),
            ]
        )
    finally:
        stop.set()
        t.join(timeout=1.0)

    assert rc == 0
    data = json.loads(out_path.read_text())
    assert data["label"] == "control"
    assert data["values"] == {"0x1100": "0a0b", "0x1102": "ff"}
    # 0x1101 never answered — absent, not a null/empty entry.
    assert "0x1101" not in data["values"]


# ---------------------------------------------------------------------------
# --batch > 1 against an ECU that refuses multi-DID reads
# ---------------------------------------------------------------------------


def _widths_snapshot(path: Path, values: dict[int, bytes]) -> Path:
    path.write_text(
        json.dumps(
            {
                "label": "discovery",
                "captured_at": 1.0,
                "values": {f"0x{did:04X}": data.hex() for did, data in values.items()},
            }
        )
    )
    return path


def _sweep_with_batch(
    tmp_path: Path,
    channel: str,
    values: dict[int, bytes],
    *,
    batch: int,
    reject_batches: bool,
    end: str = "0x1102",
) -> tuple[int, Path]:
    cfg_path = _write_config(tmp_path, channel)
    out_path = tmp_path / "snap.json"
    widths = _widths_snapshot(tmp_path / "widths.json", values)
    stop = threading.Event()
    ready = threading.Event()
    t = threading.Thread(
        target=_run_fake_ecu,
        args=(channel, values),
        kwargs={"stop": stop, "ready": ready, "reject_batches": reject_batches},
        daemon=True,
    )
    t.start()
    assert ready.wait(timeout=5.0), "fake ECU never came up"
    try:
        rc = did_sweep.main(
            [
                "--config",
                str(cfg_path),
                "--start",
                "0x1100",
                "--end",
                end,
                "--label",
                "control",
                "--session",
                "none",
                "--read-timeout",
                "0.2",
                "--batch",
                str(batch),
                "--widths-from",
                str(widths),
                "--out",
                str(out_path),
            ]
        )
    finally:
        stop.set()
        t.join(timeout=1.0)
    return rc, out_path


def test_main_fails_when_the_ecu_rejects_batched_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The sweep still gets the values — one at a time — but it must not report
    success, because nothing shares a sample instant. Silence here is what let
    a refused-batch sweep be recorded as "batched Mode 22 verified"."""
    values = {0x1100: bytes([0x0A]), 0x1102: bytes([0xFF])}
    rc, out_path = _sweep_with_batch(
        tmp_path, "did-sweep-batch-nrc", values, batch=2, reject_batches=True
    )
    assert rc == did_sweep.BATCH_NOT_HONOURED_EXIT
    err = capsys.readouterr().err
    assert "0x12" in err
    assert "subFunctionNotSupported" in err
    assert "unbatched" in err
    # The data is not lost, and the snapshot says how it was read.
    data = json.loads(out_path.read_text())
    assert data["values"] == {"0x1100": "0a", "0x1102": "ff"}
    assert data["batch_requested"] == 2
    assert data["batch_effective"] == 1
    assert data["batch_rejections"] == 1
    assert data["batch_nrc"] == "0x12"
    assert data["batch_nrc_name"] == "subFunctionNotSupported"
    assert data["batched_reads"] == 0


def test_main_succeeds_when_the_ecu_really_batches(tmp_path: Path) -> None:
    values = {0x1100: bytes([0x0A]), 0x1102: bytes([0xFF])}
    rc, out_path = _sweep_with_batch(
        tmp_path, "did-sweep-batch-ok", values, batch=2, reject_batches=False
    )
    assert rc == 0
    data = json.loads(out_path.read_text())
    assert data["values"] == {"0x1100": "0a", "0x1102": "ff"}
    assert data["batch_effective"] == 2
    assert data["batch_rejections"] == 0
    assert data["batched_reads"] == 2
    assert data["batch_nrc"] is None


# ---------------------------------------------------------------------------
# _batch_problem — which kind of "not batched" it was
# ---------------------------------------------------------------------------


def _snapshot(**kwargs: object) -> DidSnapshot:
    defaults: dict[str, object] = {
        "values": {0x1100: b"\x0a"},
        "captured_at": 1.0,
        "batch_requested": 3,
        "batch_effective": 3,
        "batch_rejections": 0,
        "batched_reads": 1,
    }
    defaults.update(kwargs)
    return DidSnapshot(**defaults)  # type: ignore[arg-type]


def test_batch_problem_is_none_when_batching_held() -> None:
    snap = _snapshot()
    assert did_sweep._batch_problem(snap, requested=3, out=Path("snap.json")) is None
    # --batch 1 never had a batch to honour.
    assert (
        did_sweep._batch_problem(
            _snapshot(batch_requested=1, batch_effective=1), requested=1, out=Path("s")
        )
        is None
    )


def test_batch_problem_names_the_nrc_for_a_refusing_ecu() -> None:
    snap = _snapshot(batch_effective=1, batch_rejections=2, batch_nrc=0x12, batched_reads=0)
    problem = did_sweep._batch_problem(snap, requested=3, out=Path("snap.json"))
    assert problem is not None
    assert "0x12" in problem and "subFunctionNotSupported" in problem
    assert "unbatched" in problem and "snap.json" in problem


def test_batch_problem_distinguishes_silence_from_a_refusal() -> None:
    snap = _snapshot(batch_effective=1, batch_rejections=1, batch_nrc=None, batched_reads=0)
    problem = did_sweep._batch_problem(snap, requested=3, out=Path("snap.json"))
    assert problem is not None
    assert "no response" in problem


def test_batch_problem_distinguishes_no_measured_widths() -> None:
    """Nothing batched, but the ECU never refused anything — a different empty."""
    snap = _snapshot(batch_effective=1, batch_rejections=0, batched_reads=0)
    problem = did_sweep._batch_problem(snap, requested=3, out=Path("snap.json"))
    assert problem is not None
    assert "measured width" in problem
    assert "NRC" not in problem


def test_batch_problem_none_on_an_old_snapshot_without_batch_fields() -> None:
    old = DidSnapshot(values={}, captured_at=1.0)
    assert did_sweep._batch_problem(old, requested=3, out=Path("snap.json")) is None

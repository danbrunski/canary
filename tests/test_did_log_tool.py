"""Tests for tools/did_log.py — the continuous, time-resolved Mode 22 DID logger."""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from src.collection.isotp import split_batched_did_response
from tools import did_log
from tools.did_log import PassiveStatus

ARMED = PassiveStatus(True, None, "armed")
DISARMED = PassiveStatus(False, None, "passive mode is off")


class FakeTransport:
    """Answers from a fixed DID table; ``drop_from_batch`` DIDs never appear in a
    batched reading (but do answer individually)."""

    def __init__(self, values: dict[int, bytes], *, drop_from_batch: set[int] | None = None):
        self.values = values
        self.drop_from_batch = drop_from_batch or set()
        self.single_calls: list[int] = []
        self.batch_calls: list[list[int]] = []

    def read_did(self, did: int) -> bytes | None:
        self.single_calls.append(did)
        return self.values.get(did)

    def read_dids(
        self, dids: Sequence[int], byte_length_for: Callable[[int], int | None]
    ) -> tuple[dict[int, bytes], bool]:
        self.batch_calls.append(list(dids))
        # Build a real concatenated response and walk it with the production
        # splitter, so a width mismatch would actually desync like on the bus.
        payload = b""
        for did in dids:
            if did in self.drop_from_batch or did not in self.values:
                continue
            payload += bytes([(did >> 8) & 0xFF, did & 0xFF]) + self.values[did]
        if not payload:
            return {}, False
        return split_batched_did_response(payload, byte_length_for)


class FakeClock:
    """Monotonic + wall clock that advance a fixed step per call."""

    def __init__(self, step: float = 0.1, wall0: float = 1_800_000_000.0) -> None:
        self.step = step
        self.t = 0.0
        self.wall0 = wall0

    def monotonic(self) -> float:
        self.t += self.step
        return self.t

    def wall(self) -> float:
        return self.wall0 + self.t


def _lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines()]


VALUES = {
    0x1101: bytes([0x01]),
    0x1201: bytes([0x0A, 0x1B]),
    0x1202: bytes([0x7F]),
    0x1207: bytes([0x00, 0x10]),
}


# ---------------------------------------------------------------------------
# run() — the logging loop, no CAN bus
# ---------------------------------------------------------------------------


def test_run_writes_wellformed_header_read_pass_summary() -> None:
    transport = FakeTransport(VALUES)
    clock = FakeClock(step=0.1)
    buf = io.StringIO()
    widths = {d: len(v) for d, v in VALUES.items()}
    summary = did_log.run(
        transport,
        widths,
        buf,
        duration_seconds=3.0,
        batch=3,
        session=0xC0,
        label="drive",
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    lines = _lines(buf)
    header = lines[0]
    assert header["kind"] == "header"
    assert header["tool"] == "did_log"
    assert header["request_id"] == "0x7E0"
    assert header["session"] == "0xC0"
    assert header["range"] == ["0x1100", "0x12FF"]
    assert header["batch"] == 3
    assert header["label"] == "drive"
    assert header["widths"] == {"0x1101": 1, "0x1201": 2, "0x1202": 1, "0x1207": 2}
    assert isinstance(header["started_at"], float)

    reads = [line for line in lines if line["kind"] == "read"]
    passes = [line for line in lines if line["kind"] == "pass"]
    assert reads and passes
    for read in reads:
        assert set(read) == {"kind", "t_send", "t_recv", "batch", "values", "missing"}
        assert read["t_recv"] >= read["t_send"]
        assert read["missing"] == []
    assert reads[0]["batch"] is True
    assert reads[0]["values"] == {"0x1101": "01", "0x1201": "0a1b", "0x1202": "7f"}
    assert reads[1]["values"] == {"0x1207": "0010"}
    assert [p["n"] for p in passes] == list(range(1, len(passes) + 1))
    assert all(p["t_end"] >= p["t_start"] for p in passes)

    assert lines[-1] == summary
    assert summary["kind"] == "summary"
    assert summary["reason"] == "duration"
    assert summary["passes"] == len(passes)
    assert summary["requests"] == len(reads)
    assert summary["reads_ok"] == sum(len(r["values"]) for r in reads)
    assert summary["reads_missing"] == 0
    assert summary["elapsed_s"] >= 3.0
    # Every request was batched: all four DIDs have measured widths.
    assert transport.single_calls == []


def test_run_rereads_a_did_missing_from_a_batch_individually() -> None:
    transport = FakeTransport(VALUES, drop_from_batch={0x1201})
    clock = FakeClock(step=0.1)
    buf = io.StringIO()
    widths = {d: len(v) for d, v in VALUES.items()}
    # Short duration: the first pass is 2 batch groups + 1 individual re-read.
    summary = did_log.run(
        transport,
        widths,
        buf,
        duration_seconds=0.35,
        batch=3,
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    reads = [line for line in _lines(buf) if line["kind"] == "read"]
    first_batch = reads[0]
    assert first_batch["batch"] is True
    assert "0x1201" not in first_batch["values"]
    assert first_batch["missing"] == ["0x1201"]
    reread = reads[1]
    assert reread["batch"] is False
    assert reread["values"] == {"0x1201": "0a1b"}
    assert reread["missing"] == []
    assert 0x1201 in transport.single_calls
    assert summary["batch_gaps"] >= 1
    assert summary["reads_missing"] == 0


def test_run_records_a_dead_did_as_missing_without_crashing() -> None:
    transport = FakeTransport({0x1101: bytes([0x01])})
    clock = FakeClock(step=0.1)
    buf = io.StringIO()
    summary = did_log.run(
        transport,
        {0x1101: 1, 0x1102: 1},  # 0x1102 no longer answers
        buf,
        duration_seconds=0.5,
        batch=2,
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    reads = [line for line in _lines(buf) if line["kind"] == "read"]
    singles = [r for r in reads if not r["batch"]]
    assert singles and all(r["missing"] == ["0x1102"] for r in singles)
    assert summary["reads_missing"] == len(singles)


def test_run_stops_with_passive_disarmed_when_checker_flips() -> None:
    transport = FakeTransport(VALUES)
    clock = FakeClock(step=1.0)
    buf = io.StringIO()
    calls = {"n": 0}

    def checker() -> PassiveStatus:
        calls["n"] += 1
        return ARMED if calls["n"] < 3 else DISARMED

    summary = did_log.run(
        transport,
        {d: len(v) for d, v in VALUES.items()},
        buf,
        duration_seconds=10_000.0,
        batch=3,
        passive_checker=checker,
        passive_check_seconds=5.0,
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    assert summary["reason"] == "passive_disarmed"
    assert "passive mode is off" in summary["detail"]
    assert calls["n"] == 3
    assert _lines(buf)[-1]["kind"] == "summary"


def test_run_stops_when_passive_ttl_is_below_check_interval() -> None:
    clock = FakeClock(step=1.0)
    buf = io.StringIO()
    summary = did_log.run(
        FakeTransport(VALUES),
        {d: len(v) for d, v in VALUES.items()},
        buf,
        duration_seconds=10_000.0,
        passive_checker=lambda: PassiveStatus(True, 3.0, "armed"),
        passive_check_seconds=5.0,
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    assert summary["reason"] == "passive_disarmed"
    assert "TTL" in summary["detail"]
    assert summary["requests"] == 0


def test_run_stops_on_signal_event() -> None:
    stop = threading.Event()
    stop.set()
    clock = FakeClock()
    buf = io.StringIO()
    summary = did_log.run(
        FakeTransport(VALUES),
        {d: len(v) for d, v in VALUES.items()},
        buf,
        duration_seconds=10_000.0,
        stop_event=stop,
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    assert summary["reason"] == "signal"


def test_discovery_widths_feed_batching() -> None:
    transport = FakeTransport(VALUES)
    widths = did_log.discover_widths(transport, 0x1100, 0x12FF)
    assert widths == {0x1101: 1, 0x1201: 2, 0x1202: 1, 0x1207: 2}
    # Discovery is unbatched.
    assert transport.batch_calls == []
    assert len(transport.single_calls) == 0x200

    transport.single_calls.clear()
    clock = FakeClock(step=0.1)
    did_log.run(
        transport,
        widths,
        io.StringIO(),
        duration_seconds=0.35,
        batch=3,
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    assert transport.batch_calls[:2] == [[0x1101, 0x1201, 0x1202], [0x1207]]
    assert transport.single_calls == []


# ---------------------------------------------------------------------------
# load_widths
# ---------------------------------------------------------------------------


def test_load_widths_from_did_sweep_snapshot(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text(
        json.dumps(
            {"label": "control", "captured_at": 1.0, "values": {"0x1201": "0a1b", "0x1101": "ff"}},
            indent=2,
        )
    )
    assert did_log.load_widths(snap) == {0x1201: 2, 0x1101: 1}


def test_load_widths_from_previous_did_log_header(tmp_path: Path) -> None:
    buf = io.StringIO()
    clock = FakeClock()
    did_log.run(
        FakeTransport(VALUES),
        {d: len(v) for d, v in VALUES.items()},
        buf,
        duration_seconds=0.5,
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    prior = tmp_path / "prior.jsonl"
    prior.write_text(buf.getvalue())
    assert did_log.load_widths(prior) == {d: len(v) for d, v in VALUES.items()}


def test_load_widths_rejects_unrelated_json(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.json"
    bogus.write_text(json.dumps({"hello": "world"}))
    with pytest.raises(ValueError):
        did_log.load_widths(bogus)


# ---------------------------------------------------------------------------
# main() — passive-mode preflight and wiring, fake transport
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_bus(monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    transport = FakeTransport(VALUES)
    opened: list[dict[str, Any]] = []

    @contextmanager
    def _open(config_path: Path, **kwargs: Any) -> Iterator[FakeTransport]:
        opened.append(kwargs)
        yield transport

    monkeypatch.setattr(did_log, "_open_transport", _open)
    transport.opened = opened  # type: ignore[attr-defined]
    return transport


def _main_args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--config",
        str(tmp_path / "unused.yaml"),
        "--session",
        "none",
        "--duration",
        "0.05",
        "--out",
        str(tmp_path / "log.jsonl"),
        *extra,
    ]


def test_main_refuses_when_passive_not_armed(
    tmp_path: Path,
    fake_bus: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    def _checker(url: str, token: str | None, **_: Any) -> Callable[[], PassiveStatus]:
        seen["url"], seen["token"] = url, token
        return lambda: DISARMED

    monkeypatch.setenv("CANARY_CONTROL_TOKEN", "sekrit")
    monkeypatch.setattr(did_log, "http_passive_checker", _checker)
    rc = did_log.main(_main_args(tmp_path))
    assert rc != 0
    err = capsys.readouterr().err
    assert "refusing" in err
    assert "Mode 01" in err
    assert "10 81" in err
    assert seen == {"url": "http://127.0.0.1:8000", "token": "sekrit"}
    # Nothing was sent and nothing written.
    assert fake_bus.opened == []  # type: ignore[attr-defined]
    assert not (tmp_path / "log.jsonl").exists()


def test_main_no_daemon_skips_the_passive_check(
    tmp_path: Path, fake_bus: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_: Any, **__: Any) -> Callable[[], PassiveStatus]:
        raise AssertionError("passive check must not run with --no-daemon")

    monkeypatch.setattr(did_log, "http_passive_checker", _boom)
    rc = did_log.main(_main_args(tmp_path, "--no-daemon", "--start", "0x1200", "--end", "0x1210"))
    assert rc == 0
    lines = _lines(io.StringIO((tmp_path / "log.jsonl").read_text()))
    assert lines[0]["kind"] == "header"
    assert lines[0]["daemon_checked"] is False
    assert lines[0]["widths"] == {"0x1201": 2, "0x1202": 1, "0x1207": 2}
    assert lines[-1]["kind"] == "summary"


def test_main_armed_runs_and_uses_widths_from_snapshot(
    tmp_path: Path, fake_bus: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(did_log, "http_passive_checker", lambda *a, **k: lambda: ARMED)
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"label": "c", "captured_at": 1.0, "values": {"0x1201": "0a1b"}}))
    rc = did_log.main(_main_args(tmp_path, "--widths-from", str(snap)))
    assert rc == 0
    lines = _lines(io.StringIO((tmp_path / "log.jsonl").read_text()))
    assert lines[0]["widths"] == {"0x1201": 2}
    assert lines[0]["daemon_checked"] is True
    # --widths-from skipped discovery: no unbatched sweep over the range.
    assert len(fake_bus.single_calls) < 0x200
    assert lines[-1]["reason"] == "duration"


def test_main_rejects_batch_above_max(tmp_path: Path, fake_bus: FakeTransport) -> None:
    rc = did_log.main(_main_args(tmp_path, "--no-daemon", "--batch", "99"))
    assert rc == 1


# ---------------------------------------------------------------------------
# http_passive_checker — response-shape parsing
# ---------------------------------------------------------------------------


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def test_http_checker_parses_control_passive_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _urlopen(request: Any, timeout: float) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        body = {
            "passive": {
                "enabled": True,
                "source": "control",
                "suppressed_frames": 4,
                "ttl_remaining_seconds": 900.0,
            }
        }
        return _FakeResponse(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    status = did_log.http_passive_checker("http://pi:8000/", "tok")()
    assert status == PassiveStatus(True, 900.0, "armed")
    assert captured == {"url": "http://pi:8000/control/passive", "auth": "Bearer tok"}


def test_http_checker_unreachable_reads_as_not_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _urlopen(request: Any, timeout: float) -> _FakeResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    status = did_log.http_passive_checker("http://127.0.0.1:8000", None)()
    assert status.armed is False
    assert "connection refused" in status.detail

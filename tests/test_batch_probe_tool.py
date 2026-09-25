"""tools/batch_probe.py — which same-instant multi-value read does an ECM serve?

The fake ECM here is a table of ``(service, body) -> RawReply``; anything not
in the table is silent. That lets each verdict — works / gated / unsupported /
silent — be pinned as its own case, and lets the two structural rules be
asserted: a positive ``0x2A`` is always followed by the stop, and no ``0x27``
sendKey can be built at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.collection.can_obd_client import RAW_QUERY_SERVICES, RawReply
from src.control.passive_client import PassiveStatus
from tools import _bus_tool, batch_probe
from tools.batch_probe import ANSWERED, GATED, SILENT, UNSUPPORTED, WORKS, classify, run


class FakeEcm:
    def __init__(
        self, table: dict[tuple[int, bytes], RawReply], *, session_ok: bool = True
    ) -> None:
        self.table = table
        self.session_ok = session_ok
        self.sent: list[tuple[int, bytes]] = []
        self.sessions: list[int] = []

    def query_raw(self, service: int, body: bytes, *, request_id: int | None = None) -> RawReply:
        if service not in RAW_QUERY_SERVICES:
            raise ValueError("would never be sent")
        self.sent.append((service, bytes(body)))
        return self.table.get((service, bytes(body)), RawReply(sent=True))

    def enter_diagnostic_session(self, subfunction: int, *, request_id: int | None = None) -> bool:
        self.sessions.append(subfunction)
        return self.session_ok


def _run(ecm: FakeEcm, **kw: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "request_id": 0x7E0,
        "session_subfunction": 0xC0,
        "lid_range": (0x00, 0x03),
        "address": 0xFFFF8000,
        "length": 8,
        "clock": lambda: 0.0,
        "sleep": lambda _s: None,
    }
    defaults.update(kw)
    return run(ecm, **defaults)


# --- classification: four kinds of empty ------------------------------------


def test_classify_names_each_kind_of_empty() -> None:
    assert classify(RawReply(sent=True, payload=b"\x01\x02"))[0] == WORKS
    assert classify(RawReply(sent=True, negative_response_code=0x11))[0] == UNSUPPORTED
    assert classify(RawReply(sent=True, negative_response_code=0x33))[0] == GATED
    assert classify(RawReply(sent=True))[0] == SILENT
    assert classify(RawReply(sent=False))[0] == SILENT
    # requestOutOfRange means the service answered — a fact about the service,
    # but not a payload, so it is never `works`.
    assert classify(RawReply(sent=True, negative_response_code=0x31))[0] == ANSWERED
    assert classify(RawReply(sent=True, negative_response_code=0x13))[0] == ANSWERED


def test_an_argument_refusal_is_not_a_same_instant_candidate() -> None:
    """0x23 answering 0x13 to every form proves the service exists and nothing
    else; listing it as a candidate would send someone building on a refusal."""
    ecm = FakeEcm(
        {
            (0x23, body): RawReply(sent=True, negative_response_code=0x13)
            for body in (
                bytes([0xFF, 0x80, 0x00, 0x08]),
                bytes([0xFF, 0xFF, 0x80, 0x00, 0x00, 0x08]),
                bytes([0x14, 0xFF, 0xFF, 0x80, 0x00, 0x08]),
            )
        }
    )
    snap = _run(ecm, skip=frozenset({"0x21", "0x2A", "0x27"}))
    assert snap["summary"]["0x23"] == ANSWERED
    assert snap["same_instant_candidates"] == []


# --- 0x21 sweep --------------------------------------------------------------


def test_local_identifier_sweep_reports_hits_and_flags_multi_frame() -> None:
    block = bytes(range(20))  # > 6 bytes: only a multi-frame response carries this
    ecm = FakeEcm(
        {
            (0x21, b"\x01"): RawReply(sent=True, payload=b"\x01" + b"\xaa\xbb"),
            (0x21, b"\x02"): RawReply(sent=True, payload=b"\x02" + block),
            (0x21, b"\x00"): RawReply(sent=True, negative_response_code=0x12),
        }
    )
    snap = _run(ecm)
    assert snap["summary"]["0x21"] == WORKS
    hits = snap["probes"]["0x21"]["hits"]
    assert [h["extra"]["local_id"] for h in hits] == ["0x01", "0x02"]
    assert "multi-frame" in hits[1]["detail"] and "multi-frame" not in hits[0]["detail"]
    assert snap["probes"]["0x21"]["overall"]["extra"] == {
        "hits": 2,
        "nrcs": {"0x12": 1},
        "silent": 1,
    }
    assert "0x21" in snap["same_instant_candidates"]


def test_local_identifier_sweep_with_only_refusals_is_unsupported_not_silent() -> None:
    ecm = FakeEcm(
        {(0x21, bytes([i])): RawReply(sent=True, negative_response_code=0x11) for i in range(4)}
    )
    snap = _run(ecm)
    assert snap["summary"]["0x21"] == UNSUPPORTED
    assert snap["same_instant_candidates"] == []


def test_local_identifier_sweep_gated_wins_over_unsupported() -> None:
    ecm = FakeEcm(
        {
            (0x21, b"\x00"): RawReply(sent=True, negative_response_code=0x11),
            (0x21, b"\x01"): RawReply(sent=True, negative_response_code=0x33),
        }
    )
    assert _run(ecm)["summary"]["0x21"] == GATED


def test_session_is_primed_before_the_sweep_and_refreshed() -> None:
    clock_now = [0.0]
    ecm = FakeEcm({})
    _run(ecm, clock=lambda: clock_now[0], lid_range=(0x00, 0x01))
    assert ecm.sessions[:1] == [0xC0]
    assert ecm.sent[0][0] == 0x21


# --- 0x2A: never leave the ECM streaming -------------------------------------


def test_periodic_positive_is_always_followed_by_the_stop_request() -> None:
    ecm = FakeEcm({(0x2A, b"\x01\x01"): RawReply(sent=True, payload=b"\x01")})
    snap = _run(ecm, skip=frozenset({"0x21", "0x23", "0x27"}))
    sent_2a = [body for svc, body in ecm.sent if svc == 0x2A]
    assert sent_2a == [b"\x01\x01", b"\x04"]
    assert snap["probes"]["0x2A"][1]["request"] == "2A 04 (stop)"
    assert snap["summary"]["0x2A"] == WORKS


def test_periodic_refusal_sends_no_stop() -> None:
    ecm = FakeEcm({(0x2A, b"\x01\x01"): RawReply(sent=True, negative_response_code=0x11)})
    snap = _run(ecm, skip=frozenset({"0x21", "0x23", "0x27"}))
    assert [body for svc, body in ecm.sent if svc == 0x2A] == [b"\x01\x01"]
    assert snap["summary"]["0x2A"] == UNSUPPORTED


# --- 0x23 and 0x27: gated is reported, no key is ever built --------------------


def test_memory_read_tries_all_three_forms_and_gated_shows_through() -> None:
    kwp24 = bytes([0xFF, 0x80, 0x00, 0x08])
    kwp32 = bytes([0xFF, 0xFF, 0x80, 0x00, 0x00, 0x08])
    uds = bytes([0x14, 0xFF, 0xFF, 0x80, 0x00, 0x08])
    ecm = FakeEcm(
        {
            (0x23, kwp24): RawReply(sent=True, negative_response_code=0x12),
            (0x23, kwp32): RawReply(sent=True, negative_response_code=0x13),
            (0x23, uds): RawReply(sent=True, negative_response_code=0x33),
            (0x27, b"\x01"): RawReply(sent=True, payload=b"\x01\xde\xad\xbe\xef"),
        }
    )
    snap = _run(ecm, skip=frozenset({"0x21", "0x2A"}))
    assert [r["extra"]["form"] for r in snap["probes"]["0x23"]] == ["kwp24", "kwp32", "uds"]
    # 0x33 on one form outranks 0x13 (answered, wrong form) and 0x12 elsewhere.
    assert snap["summary"]["0x23"] == GATED
    assert snap["summary"]["0x27"] == WORKS
    assert "no key sent" in snap["probes"]["0x27"]["detail"]
    assert snap["probes"]["0x27"]["payload_hex"] == "01deadbeef"
    # The only 0x27 ever sent is requestSeed.
    assert [body for svc, body in ecm.sent if svc == 0x27] == [b"\x01"]


def test_the_d03_17_memory_read_form_is_sent_byte_for_byte() -> None:
    """D03.17 records `23 00 08 00 00 00 10` (4-byte address, 2-byte size)
    answering under session 10 FB. That exact body must go on the wire."""
    ecm = FakeEcm({})
    _run(ecm, skip=frozenset({"0x21", "0x2A", "0x27"}), address=0x00080000, length=16)
    bodies = [body for svc, body in ecm.sent if svc == 0x23]
    assert bytes([0x00, 0x08, 0x00, 0x00, 0x00, 0x10]) in bodies


def test_a_send_key_cannot_be_built_through_the_client() -> None:
    """Structural: the client refuses an even 0x27 sub-function before any
    frame exists, so no tool can send one whatever it asks for."""
    from src.collection.can_obd_client import CanObdClient

    class _Bus:
        def send(self, *_a, **_k):  # pragma: no cover - must never be reached
            raise AssertionError("a key was sent")

    client = CanObdClient(_Bus())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requestSeed only"):
        client.query_raw(0x27, b"\x02\x12\x34")
    with pytest.raises(ValueError, match="not a probe read service"):
        client.query_raw(0x2E, b"\x12\x01\xff")  # WriteDataByIdentifier
    with pytest.raises(ValueError, match="not a probe read service"):
        client.query_raw(0x34, b"")  # RequestDownload
    assert 0x2E not in RAW_QUERY_SERVICES and 0x3D not in RAW_QUERY_SERVICES


# --- snapshot + CLI -----------------------------------------------------------


def test_snapshot_shape_and_summary_text() -> None:
    ecm = FakeEcm({(0x21, b"\x01"): RawReply(sent=True, payload=b"\x01\x02")})
    snap = _run(ecm)
    assert snap["tool"] == "batch_probe"
    assert snap["session"] == "0xC0" and snap["request_id"] == "0x7E0"
    assert set(snap["summary"]) == {"0x21", "0x2A", "0x23", "0x27"}
    text = batch_probe.format_summary(snap)
    assert "0x21     works" in text
    assert "same-instant multi-value read: 0x21" in text
    json.dumps(snap)  # serialisable


def test_main_refuses_when_passive_is_not_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        _bus_tool,
        "http_passive_checker",
        lambda *a, **k: lambda: PassiveStatus(False, None, "passive mode is off"),
    )

    def _never(*_a, **_k):  # pragma: no cover
        raise AssertionError("the bus was opened despite the refusal")

    monkeypatch.setattr(batch_probe, "open_client", _never)
    rc = batch_probe.main(["--out", str(tmp_path / "p.json"), "--config", "x.yaml"])
    assert rc == 2
    assert "refusing" in capsys.readouterr().err
    assert not (tmp_path / "p.json").exists()


def test_main_rejects_unknown_skip_and_bad_lid_range(tmp_path: Path) -> None:
    out = str(tmp_path / "p.json")
    assert batch_probe.main(["--out", out, "--no-daemon", "--skip", "0x99"]) == 1
    assert batch_probe.main(["--out", out, "--no-daemon", "--lid-end", "0x100"]) == 1

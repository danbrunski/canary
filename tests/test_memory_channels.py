"""Service-0x23 (ReadMemoryByAddress) channels, end to end.

The quantity that forced this path is the ECM's final commanded
equivalence ratio. It is not a DID — UpRev reads it from two absolute RAM
bytes — which is why 138 answering DIDs and a full broadcast sweep never
located it (data/analyses/2026-09-20-uprev-usb-road.md).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from src.analysis.channels import parse_spec, read_channels
from src.analysis.tuning_frame import Provider, build_tuning_frame
from src.collection.can_obd_client import ObdResponse
from src.collection.can_obd_collector import _decode_response
from src.collection.isotp import build_memory_read_payload
from src.collection.obd_scheduler import CanObdSpec, _PollRequest, build_poll_requests
from src.collection.request_eligibility import SESSION_PHASES, evaluate
from src.config import CanBusConfig
from src.decode.dids import (
    DidMap,
    MemoryChannel,
    apply_memory_scaling,
    decode_memory_raw,
    is_memory_sentinel,
    memory_channels_for_revision,
    plan_memory_reads,
)

_TARGET = MemoryChannel(
    name="ECM_TARGET_EQUIV_RATIO",
    address=0xFFFF26BE,
    length=2,
    byte_length=2,
    scale=1 / 16384,
    unit="phi",
    confidence="high",
    verified_tune_revision="ZS44A-k106",
    sentinel_raw=0,
    sentinel_meaning="fuel cut / no AFR target",
)


def test_request_frame_matches_the_form_verified_on_the_vehicle() -> None:
    """KWP32 body. KWP24 and UDS-ALFID both answered NRC 0x12 on this ECM."""
    assert build_memory_read_payload(0xFFFF26BE, 2) == bytes.fromhex("0723ffff26be0002")


@pytest.mark.parametrize(
    ("address", "length"),
    [(0x1_0000_0000, 2), (-1, 2), (0xFFFF26BE, 0), (0xFFFF26BE, 0x10000)],
)
def test_request_frame_refuses_out_of_range(address: int, length: int) -> None:
    with pytest.raises(ValueError):
        build_memory_read_payload(address, length)


@pytest.mark.parametrize(
    ("word", "expected_phi"),
    [(0x4000, 1.0), (0x4A80, 1.1640625), (0x4380, 1.0546875)],
)
def test_q14_word_decodes_to_the_documented_phi(word: int, expected_phi: float) -> None:
    raw = decode_memory_raw(_TARGET, word.to_bytes(2, "big"))
    assert raw == word
    assert apply_memory_scaling(_TARGET, raw) == pytest.approx(expected_phi)


def test_known_answer_target_afr() -> None:
    """0x4A80 is a 12.63 AFR target at gasoline stoich — the pinned value."""
    phi = apply_memory_scaling(_TARGET, 0x4A80)
    assert phi is not None
    assert 14.7 / phi == pytest.approx(12.63, abs=0.01)


def test_sentinel_decodes_to_nothing_rather_than_zero() -> None:
    """Scaling the fuel-cut word gives phi 0.0, which divides into infinity."""
    assert is_memory_sentinel(_TARGET, 0) is True
    assert apply_memory_scaling(_TARGET, 0) is None


def test_sentinel_needs_a_stated_meaning() -> None:
    with pytest.raises(ValidationError, match="sentinel_raw and sentinel_meaning"):
        MemoryChannel(
            name="X",
            address=0x1000,
            length=2,
            byte_length=2,
            confidence="low",
            verified_tune_revision="rev",
            sentinel_raw=0,
        )


def test_decoded_slice_must_fit_the_read() -> None:
    with pytest.raises(ValidationError, match="does not fit"):
        MemoryChannel(
            name="X",
            address=0x1000,
            length=2,
            byte_start=1,
            byte_length=2,
            confidence="low",
            verified_tune_revision="rev",
        )


def test_address_cannot_be_recorded_without_a_calibration() -> None:
    """The whole difference between a DID and an address."""
    with pytest.raises(ValidationError):
        MemoryChannel(name="X", address=0x1000, length=2, byte_length=2, confidence="low")


@pytest.mark.parametrize(
    ("running", "usable"),
    [("ZS44A-k106", True), ("ZS44A-k105", False), (None, False)],
)
def test_channel_is_withheld_on_another_calibration(running: str | None, usable: bool) -> None:
    ok, withheld = memory_channels_for_revision([_TARGET], running)
    assert bool(ok) is usable
    assert bool(withheld) is not usable


@pytest.mark.parametrize("phase", SESSION_PHASES)
def test_memory_is_gated_exactly_like_mode_22(phase: str) -> None:
    """Same diagnostic session, so it must not sit on the Mode 01-side row.

    On the Mode 01-side row every read would be issued in the one phase
    where this ECM is silent about service 0x23.
    """
    assert evaluate("memory", "default", phase) == evaluate("did", "default", phase)


def test_scheduler_builds_a_session_endpoint_request() -> None:
    spec = CanObdSpec(
        can=CanBusConfig(interface="socketcan", channel="can0", bitrate=500000),
        pids=(),
        memory=(_TARGET,),
        did_request_id=0x7E0,
    )
    requests = [r for r in build_poll_requests(None, spec) if r.kind == "memory"]

    assert len(requests) == 1
    request = requests[0]
    assert request.service == 0x23
    assert request.request_id == 0x7E0
    assert request.channel == "mem:ECM_TARGET_EQUIV_RATIO"
    assert request.memory_read is not None
    assert request.memory_read.channels == (_TARGET,)


def _request() -> _PollRequest:
    return _PollRequest(
        kind="memory",
        service=0x23,
        identifier=0,
        request_id=0x7E0,
        channel="mem:ECM_TARGET_EQUIV_RATIO",
        memory_read=plan_memory_reads([_TARGET])[0],
    )


def _response(payload_hex: str) -> ObdResponse:
    return ObdResponse(
        service=0x63, pid=None, payload=bytes.fromhex(payload_hex), responder_id=0x7E8
    )


def test_decode_emits_the_engineering_value() -> None:
    response = _response("4a80")

    readings = _decode_response(_request(), response, timestamp=1.0)

    assert len(readings) == 1
    assert readings[0].pid == "mem:ECM_TARGET_EQUIV_RATIO"
    assert readings[0].value == pytest.approx(1.1640625)
    assert readings[0].unit == "phi"
    # The raw word survives so a corrected scale can be re-run over history.
    assert readings[0].payload == bytes.fromhex("4a80")


def test_fuel_cut_is_reported_not_dropped() -> None:
    """ "Polled, commanding nothing" must not look like "not polled"."""
    response = _response("0000")

    readings = _decode_response(_request(), response, timestamp=1.0)

    assert [r.pid for r in readings] == ["mem:ECM_TARGET_EQUIV_RATIO_SENTINEL"]
    assert "fuel cut" in str(readings[0].value)


def test_short_response_decodes_to_nothing() -> None:
    response = _response("40")
    assert _decode_response(_request(), response, timestamp=1.0) == []


# --------------------------------------------------------------------------
# Analysis path: reading `mem:` channels and turning phi into a commanded AFR.
# --------------------------------------------------------------------------


def _session_frame() -> pd.DataFrame:
    """Rows shaped like a stored session: one reading, one fuel-cut state."""
    return pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-09-20T22:36:28Z"),
                "source": "obd",
                "channel": "mem:ECM_TARGET_EQUIV_RATIO",
                "value_number": 1.1640625,
                "value_text": None,
                "unit": "phi",
                "value_bool": None,
                "payload": None,
                "metadata_json": None,
            },
            {
                "timestamp": pd.Timestamp("2026-09-20T22:36:29Z"),
                "source": "obd",
                "channel": "mem:ECM_TARGET_EQUIV_RATIO_SENTINEL",
                "value_number": None,
                "value_text": "fuel cut / no AFR target",
                "unit": "",
                "value_bool": None,
                "payload": None,
                "metadata_json": None,
            },
        ]
    )


def test_mem_namespace_parses() -> None:
    assert parse_spec("mem:ECM_TARGET_EQUIV_RATIO").source == "mem"


def test_reads_the_scaled_value_from_a_session() -> None:
    out = read_channels(_session_frame(), ["mem:ECM_TARGET_EQUIV_RATIO"])

    assert len(out) == 1
    assert out.iloc[0]["value"] == pytest.approx(1.1640625)
    assert out.iloc[0]["unit"] == "phi"


def test_sentinel_reads_as_a_pairable_indicator_not_a_measurement() -> None:
    """Fuel cut must be an instant you can pair against, in units you cannot
    mistake for phi."""
    out = read_channels(_session_frame(), ["mem:ECM_TARGET_EQUIV_RATIO_SENTINEL"])

    assert len(out) == 1
    assert out.iloc[0]["value"] == 1.0
    assert out.iloc[0]["unit"] == "flag"


def test_commanded_afr_provider_converts_phi_to_afr(tmp_path: Path) -> None:
    """The role that was declared-but-absent for the whole all-DID campaign."""
    registry = tmp_path / "providers.yaml"
    registry.write_text(
        "schema_version: 1\n"
        "providers:\n"
        "  - role: commanded_afr\n"
        "    spec: 'mem:ECM_TARGET_EQUIV_RATIO'\n"
        "    transform: equiv_ratio_afr\n"
    )
    session = tmp_path / "session"
    session.mkdir()
    _session_frame().to_parquet(session / "telemetry-0001.parquet")

    frame = build_tuning_frame(session, providers_path=registry).values

    assert frame["commanded_afr"].dropna().iloc[0] == pytest.approx(12.63, abs=0.01)
    # The instrument reading survives the conversion, as it does for voltage.
    assert frame["commanded_afr_equiv_ratio"].dropna().iloc[0] == pytest.approx(1.1640625)


def test_equiv_ratio_provider_may_not_claim_a_unit() -> None:
    with pytest.raises(ValidationError, match="always produces AFR"):
        Provider(
            role="commanded_afr",
            spec="mem:X",
            transform="equiv_ratio_afr",
            unit="phi",
        )


def test_fueling_active_is_true_wherever_a_target_exists(tmp_path: Path) -> None:
    """Fuel cut is excluded by construction, not by a gate.

    The sentinel lives on a different channel, so a cut instant contributes
    no row here at all. The delivery gate built on this column therefore
    reports zero drops, and that is the honest reading rather than a gate
    that silently never fires.
    """
    registry = tmp_path / "providers.yaml"
    registry.write_text(
        "schema_version: 1\n"
        "providers:\n"
        "  - role: fueling_active\n"
        "    spec: 'mem:ECM_TARGET_EQUIV_RATIO'\n"
        "    transform: target_present\n"
    )
    session = tmp_path / "session"
    session.mkdir()
    _session_frame().to_parquet(session / "telemetry-0001.parquet")

    frame = build_tuning_frame(session, providers_path=registry).values

    assert frame["fueling_active"].tolist() == [True]


# --------------------------------------------------------------------------
# Multi-field windows: the only same-instant multi-channel read this ECM has.
# --------------------------------------------------------------------------

_PERIOD = MemoryChannel(
    name="ECM_ENGINE_PERIOD",
    address=0xFFFF26AE,
    length=2,
    byte_length=2,
    confidence="high",
    verified_tune_revision="ZS44A-k106",
    window="fuel_target",
)
_TARGET_WINDOWED = _TARGET.model_copy(update={"window": "fuel_target"})


def test_a_window_becomes_one_read_spanning_its_members() -> None:
    reads = plan_memory_reads([_TARGET_WINDOWED, _PERIOD])

    assert len(reads) == 1
    assert reads[0].address == 0xFFFF26AE
    assert reads[0].length == 18  # 0x26AE..0x26BF inclusive
    assert reads[0].offsets == (16, 0)


def test_an_unwindowed_channel_is_a_read_of_its_own() -> None:
    reads = plan_memory_reads([_TARGET, _PERIOD.model_copy(update={"window": None})])

    assert [r.length for r in reads] == [2, 2]


def test_one_reply_decodes_every_field_on_one_timestamp() -> None:
    """The point of the window: two quantities from one ECU operation.

    This ECM refuses batched Mode 22, so two DIDs are always two requests
    milliseconds apart. Anything asserting simultaneity has to come from
    here.
    """
    read = plan_memory_reads([_TARGET_WINDOWED, _PERIOD])[0]
    request = _PollRequest(
        kind="memory",
        service=0x23,
        identifier=0,
        request_id=0x7E0,
        channel="mem:fuel_target",
        memory_read=read,
    )
    # period 0x0BB8 at +0, then filler, then target 0x4A80 at +16
    payload = bytes.fromhex("0bb8") + bytes(14) + bytes.fromhex("4a80")

    readings = _decode_response(request, _response(payload.hex()), timestamp=7.0)

    assert {r.pid for r in readings} == {
        "mem:ECM_ENGINE_PERIOD",
        "mem:ECM_TARGET_EQUIV_RATIO",
    }
    assert {r.timestamp for r in readings} == {7.0}
    by_name = {r.pid: r for r in readings}
    assert by_name["mem:ECM_TARGET_EQUIV_RATIO"].value == pytest.approx(1.1640625)
    # Scale deliberately unpinned (the rpm relation is reciprocal), so raw.
    assert by_name["mem:ECM_ENGINE_PERIOD"].value == 3000.0
    assert by_name["mem:ECM_ENGINE_PERIOD"].unit == "counts"


def test_a_window_over_the_measured_ceiling_is_refused_at_load() -> None:
    """An oversized window answers NRC 0x12 for the whole group at drive
    time; this turns that into a load error."""
    far = _PERIOD.model_copy(update={"address": 0xFFFF26AE + 200, "name": "FAR"})
    with pytest.raises(ValidationError, match="over this ECM's measured 63-byte"):
        DidMap.model_validate(
            {
                "schema_version": 1,
                "access": {
                    "ecu_request_id": 0x7E0,
                    "ecu_response_id": 0x7E8,
                    "max_memory_read_length": 63,
                },
                "dids": [],
                "memory": [_TARGET_WINDOWED.model_dump(), far.model_dump()],
            }
        )


def test_a_window_may_not_mix_calibrations() -> None:
    other = _PERIOD.model_copy(update={"verified_tune_revision": "SOME-OTHER-ROM"})
    with pytest.raises(ValidationError, match="mixes calibrations"):
        DidMap.model_validate(
            {
                "schema_version": 1,
                "access": {
                    "ecu_request_id": 0x7E0,
                    "ecu_response_id": 0x7E8,
                    "max_memory_read_length": 63,
                },
                "dids": [],
                "memory": [_TARGET_WINDOWED.model_dump(), other.model_dump()],
            }
        )


def test_a_windowed_request_is_admitted_by_its_members(monkeypatch) -> None:
    """A window is named after the window, not any one channel it carries.

    Matching the profile's selection against that name finds nothing, which
    silently dropped every windowed read on the vehicle — the capture ran,
    the daemon reported the profile active, and zero `mem:` rows landed.
    """
    from src.collection import tuning_profile
    from src.collection.capture_profiles import CaptureProfile

    profile = CaptureProfile(
        name="research",
        mode22_enabled=True,
        memory=("ECM_TARGET_EQUIV_RATIO",),
        storage_prefix="_capture-research",
    )
    tuning_profile.activate(profile)
    try:
        read = plan_memory_reads([_TARGET_WINDOWED, _PERIOD])[0]
        request = _PollRequest(
            kind="memory",
            service=0x23,
            identifier=0,
            request_id=0x7E0,
            channel="mem:fuel_target",  # the window, not a channel
            memory_read=read,
        )
        assert tuning_profile.request_verdict(request).eligible is True
    finally:
        tuning_profile.set_active(False)


def test_a_windowed_request_with_no_selected_member_is_refused(monkeypatch) -> None:
    from src.collection import tuning_profile
    from src.collection.capture_profiles import CaptureProfile

    profile = CaptureProfile(
        name="research",
        mode22_enabled=True,
        memory=("SOMETHING_ELSE",),
        storage_prefix="_capture-research",
    )
    tuning_profile.activate(profile)
    try:
        read = plan_memory_reads([_TARGET_WINDOWED, _PERIOD])[0]
        request = _PollRequest(
            kind="memory",
            service=0x23,
            identifier=0,
            request_id=0x7E0,
            channel="mem:fuel_target",
            memory_read=read,
        )
        verdict = tuning_profile.request_verdict(request)
        assert verdict.eligible is False
        assert verdict.reason == "not_selected"
    finally:
        tuning_profile.set_active(False)


def test_a_capture_payload_names_the_memory_channels_it_will_not_get() -> None:
    """A withheld channel used to be one log line at boot.

    After a reflash every memory channel is withheld until re-verified, and a
    capture that silently lost all of them looks exactly like a healthy one.
    """
    from src.collection import tuning_profile
    from src.collection.capture_profiles import CaptureProfile

    tuning_profile.configure_memory_state(
        {"ECM_TARGET_EQUIV_RATIO": "ZS44A-k106"}, "ZS44A-20260920"
    )
    tuning_profile.activate(
        CaptureProfile(
            name="research",
            memory=("ECM_TARGET_EQUIV_RATIO", "ECM_ENGINE_PERIOD"),
            storage_prefix="_capture-research",
        )
    )
    try:
        payload = tuning_profile.snapshot()
        assert payload["running_tune_revision"] == "ZS44A-20260920"
        assert [w["channel"] for w in payload["memory_withheld"]] == ["ECM_TARGET_EQUIV_RATIO"]
        assert payload["memory_withheld"][0]["verified_on"] == "ZS44A-k106"
    finally:
        tuning_profile.set_active(False)
        tuning_profile.configure_memory_state({}, None)

"""Byte-sweep discovery: does it find real bindings and refuse fake ones?"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.broadcast_discovery import (
    PairingPolicy,
    Probe,
    candidate_probes,
    credible_fits,
    sweep_session,
)

#: These hand-built frames carry no ``metadata_json``, so their stamp
#: provenance is ``unknown`` and a sub-second tolerance would be refused;
#: one second is the floor, and the rows are stamped on whole seconds anyway.
POLICY = PairingPolicy.within("1s")


def _session(frames: dict[int, list[bytes]], references: dict[str, list[float]]) -> pd.DataFrame:
    """A long-format session frame: raw CAN rows plus engineering references."""
    rows: list[dict[str, object]] = []
    for can_id, payloads in frames.items():
        for index, payload in enumerate(payloads):
            rows.append(
                {
                    "timestamp": pd.Timestamp("2026-08-06T00:00:00Z") + pd.Timedelta(seconds=index),
                    "source": "can",
                    "channel": f"0x{can_id:03X}",
                    "value_number": None,
                    "value_text": None,
                    "unit": None,
                    "payload": payload,
                }
            )
    for channel, values in references.items():
        for index, value in enumerate(values):
            rows.append(
                {
                    "timestamp": pd.Timestamp("2026-08-06T00:00:00Z") + pd.Timedelta(seconds=index),
                    "source": "obd",
                    "channel": channel,
                    "value_number": value,
                    "value_text": None,
                    "unit": "rpm",
                    "payload": None,
                }
            )
    return pd.DataFrame(rows)


def _rpm_walk(n: int) -> list[float]:
    rng = np.random.default_rng(20260806)
    return list(800.0 + np.cumsum(rng.normal(0.0, 60.0, n)).clip(-400.0, 3000.0))


def test_candidate_probes_do_not_double_sweep_single_bytes() -> None:
    """Endianness is meaningless for one byte; emitting both doubles the noise."""
    probes = candidate_probes(8, max_byte_length=2)
    singles = [p for p in probes if p.byte_length == 1]
    assert len(singles) == 8
    assert {p.byte_order for p in singles} == {"big"}
    pairs = [p for p in probes if p.byte_length == 2]
    assert len(pairs) == 7 * 2  # seven positions, both orders


def test_sweep_recovers_a_planted_two_byte_signal_with_its_scale() -> None:
    """The whole method rests on this: plant rpm x 8, get scale 0.125 back."""
    rpm = _rpm_walk(400)
    payloads = [
        bytes([0x20, 0x00, (int(v * 8) >> 8) & 0xFF, int(v * 8) & 0xFF, 0, 0, 0, 0]) for v in rpm
    ]
    session = _session({0x1F9: payloads}, {"RPM": rpm})

    fits = sweep_session(session, ["obd:RPM"], policy=POLICY, min_pairs=100)
    best = credible_fits(fits)[0]

    assert best.probe == Probe(can_id=0x1F9, byte_start=2, byte_length=2, byte_order="big")
    assert best.slope == pytest.approx(0.125, rel=1e-3)
    assert best.r_squared > 0.999
    assert best.scale_verdict == "stable"


def test_sweep_rejects_a_byte_that_only_co_varies_in_part_of_the_session() -> None:
    """r² alone would promote this; scale stability is what refuses it.

    Over a single drive nearly every engine byte correlates with rpm, so a
    high r² is nearly free. The planted byte here tracks rpm at one scale for
    the first half of the session and a different scale for the second — a
    real binding cannot do that.
    """
    rpm = _rpm_walk(400)
    payloads = []
    for index, value in enumerate(rpm):
        scaled = value * (2.0 if index < len(rpm) // 2 else 6.0)
        payloads.append(bytes([(int(scaled) >> 8) & 0xFF, int(scaled) & 0xFF, 0, 0]))
    session = _session({0x233: payloads}, {"RPM": rpm})

    fits = sweep_session(session, ["obd:RPM"], policy=POLICY, min_pairs=100)
    drifter = next(
        fit
        for fit in fits
        if fit.probe == Probe(can_id=0x233, byte_start=0, byte_length=2, byte_order="big")
    )

    assert drifter.scale_verdict == "drifting"
    assert not any(fit.probe == drifter.probe for fit in credible_fits(fits))


def test_a_barely_moving_reference_is_unresolved_rather_than_drifting() -> None:
    """Unassessable is not a soft rejection, and must not read as one.

    0x551 byte 0 is the *confirmed* coolant byte and still spread 83% across
    sub-windows on the 2026-08-06 drive, purely because coolant crawls: each
    window's slope is fitted almost entirely to quantisation noise. Calling
    that "drifting" would condemn a correct binding, so the verdict has to
    distinguish "this is wrong" from "this session cannot tell".
    """
    # Warms only in the middle third: strong overall fit, but the first and
    # last windows are flat, so neither can identify a slope on its own.
    coolant = [
        80.0 if index < 133 else (95.0 if index >= 266 else 80.0 + (index - 133) * 15.0 / 133.0)
        for index in range(400)
    ]
    payloads = [bytes([int(round(value + 40.0)), 0, 0, 0]) for value in coolant]
    session = _session({0x551: payloads}, {"COOLANT_TEMP": coolant})

    fits = sweep_session(session, ["obd:COOLANT_TEMP"], policy=POLICY, min_pairs=100)
    probe = Probe(can_id=0x551, byte_start=0, byte_length=1, byte_order="big")
    fit = next(f for f in fits if f.probe == probe)

    assert fit.scale_verdict == "unresolved"
    assert not any(f.probe == probe for f in credible_fits(fits))
    assert any(f.probe == probe for f in credible_fits(fits, include_unresolved=True))


def test_a_reference_that_was_never_captured_is_named_rather_than_swept_silently() -> None:
    """A sweep against an absent reference must say so, not return nothing."""
    from src.signals.pairing import ComparisonRefusedError

    rpm = _rpm_walk(200)
    payloads = [bytes([0, 0, (int(v * 8) >> 8) & 0xFF, int(v * 8) & 0xFF, 0, 0, 0, 0]) for v in rpm]
    session = _session({0x1F9: payloads}, {"RPM": rpm})

    with pytest.raises(ComparisonRefusedError, match="obd:SPEED"):
        sweep_session(session, ["obd:RPM", "obd:SPEED"], policy=POLICY, min_pairs=100)


def test_constant_bytes_never_produce_a_fit() -> None:
    """Padding bytes correlate with nothing and must not reach the caller."""
    rpm = _rpm_walk(300)
    payloads = [bytes([0xFF, 0x00, 0xFF, 0x00]) for _ in rpm]
    session = _session({0x794: payloads}, {"RPM": rpm})

    assert sweep_session(session, ["obd:RPM"], policy=POLICY, min_pairs=100) == []


def test_grid_is_refused_in_favour_of_a_pairing_policy() -> None:
    rpm = _rpm_walk(50)
    session = _session({0x1F9: [bytes(8) for _ in rpm]}, {"RPM": rpm})
    with pytest.raises(TypeError, match="policy="):
        sweep_session(session, ["obd:RPM"], grid="1s", policy=POLICY)


def test_short_frames_are_dropped_not_zero_padded() -> None:
    """A short frame is a different message; padding invents bytes off the wire."""
    rpm = _rpm_walk(300)
    payloads = [bytes([0, 0, (int(v * 8) >> 8) & 0xFF, int(v * 8) & 0xFF, 0, 0, 0, 0]) for v in rpm]
    payloads[:20] = [b"\x00\x01" for _ in range(20)]  # a burst of 2-byte frames
    session = _session({0x1F9: payloads}, {"RPM": rpm})

    fits = sweep_session(session, ["obd:RPM"], policy=POLICY, min_pairs=100)
    best = credible_fits(fits)[0]

    assert best.probe.byte_start == 2
    assert best.slope == pytest.approx(0.125, rel=1e-3)

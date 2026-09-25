"""R05.9 — one byte-slice / scale / plausibility layer for every decode catalog."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.decode import slices
from src.decode.bindings import Signal, apply_scaling, decode_raw, is_plausible, load_bindings
from src.decode.sensor_can import SensorCanCatalog, decode_frame


def _signal(**overrides: object) -> Signal:
    fields: dict[str, object] = {
        "name": "PROBE",
        "module": "ECM",
        "can_id": 0x23D,
        "byte_start": 1,
        "byte_length": 2,
        "confidence": "high",
    }
    fields.update(overrides)
    return Signal.model_validate(fields)


def test_slice_raw_reads_either_order_and_sign() -> None:
    data = bytes([0x00, 0xFF, 0xFE, 0x01])
    assert slices.slice_raw(data, 1, 2, "big") == 0xFFFE
    assert slices.slice_raw(data, 1, 2, "little") == 0xFEFF
    assert slices.slice_raw(data, 1, 2, "big", signed=True) == -2
    assert slices.slice_raw(data, 3, 2, "big") is None  # frame too short


def test_linear_and_range_helpers() -> None:
    assert slices.linear_value(10, None, 5.0) is None  # unpinned scale
    assert slices.linear_value(10, 0.5, None) == 5.0
    assert slices.linear_factors(2.0, -40.0) == (2.0, -40.0)
    assert slices.in_range(5.0, None, None) is None
    assert slices.in_range(5.0, 0.0, None) is True
    assert slices.in_range(5.0, None, 4.0) is False


def test_bindings_decode_through_the_shared_layer() -> None:
    signal = _signal(scale=0.125, offset=-10.0, plausible_min=0.0, plausible_max=100.0)
    raw = decode_raw(signal, bytes([0, 0x01, 0x00]))
    assert raw == 256
    assert apply_scaling(signal, raw) == pytest.approx(22.0)
    assert is_plausible(signal, 22.0) is True
    assert is_plausible(signal, -1.0) is False


def test_binding_evidence_refuses_a_bare_constant() -> None:
    """``dids.Evidence`` refused a constant without its conditions and unit;
    ``bindings.Evidence`` lacked the fields, so a binding could not record
    one safely at all. Both now share one model."""
    evidence = {"session": "s", "method": "m", "finding": "f", "constant": 0.125}
    with pytest.raises(ValidationError, match="conditions and constant_unit"):
        _signal(evidence=[evidence])
    ok = _signal(evidence=[{**evidence, "constant_unit": "rpm/count", "conditions": "idle"}])
    assert ok.evidence[0].constant == 0.125


def test_the_shipped_catalogs_still_load() -> None:
    bindings = load_bindings(Path("data/bindings.yaml"))
    assert bindings.signals
    assert all(isinstance(e, slices.Evidence) for s in bindings.signals for e in s.evidence)


def test_sensor_can_decodes_signed_values_through_the_shared_slice() -> None:
    catalog = SensorCanCatalog.model_validate(
        {
            "signals": [
                {
                    "name": "BOOST",
                    "device": "gauge",
                    "source": "manual",
                    "arbitration_id": 0x100,
                    "extended": False,
                    "byte_start": 0,
                    "byte_length": 2,
                    "endianness": "big",
                    "signed": True,
                    "scale": 0.1,
                    "unit": "kPa",
                    "verified": False,
                    "valid_min": -100.0,
                    "valid_max": 300.0,
                }
            ]
        }
    )
    (reading,) = decode_frame(catalog, 0x100, False, bytes([0xFF, 0x9C]))
    assert reading.value == pytest.approx(-10.0)
    assert decode_frame(catalog, 0x100, False, bytes([0x7F, 0xFF])) == []  # out of range
    assert decode_frame(catalog, 0x100, False, bytes([0x01])) == []  # too short

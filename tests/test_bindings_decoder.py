"""Tests for the bindings loader, decoder, and CLI report tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from src.decode.bindings import (
    Signal,
    apply_scaling,
    decode_raw,
    is_plausible,
    load_bindings,
)
from tools.decode_bindings import collect_stats

# ---------------------------------------------------------------------------
# Signal schema
# ---------------------------------------------------------------------------


def test_signal_minimal_fields():
    """Just name, module, can_id, byte_start, byte_length, confidence are needed."""
    s = Signal(
        name="TEST",
        module="ECM",
        can_id=0x551,
        byte_start=0,
        byte_length=1,
        confidence="high",
    )
    assert s.scale is None
    assert s.offset is None
    assert s.unit == "unknown"
    assert s.byte_order == "big"


def test_signal_rejects_invalid_confidence():
    with pytest.raises(ValidationError):
        Signal(
            name="X",
            module="Y",
            can_id=1,
            byte_start=0,
            byte_length=1,
            confidence="totally_certain",  # type: ignore[arg-type]
        )


def test_signal_rejects_invalid_byte_order():
    with pytest.raises(ValidationError):
        Signal(
            name="X",
            module="Y",
            can_id=1,
            byte_start=0,
            byte_length=1,
            confidence="high",
            byte_order="middle",  # type: ignore[arg-type]
        )


def test_signal_rejects_byte_start_out_of_range():
    with pytest.raises(ValidationError):
        Signal(
            name="X",
            module="Y",
            can_id=1,
            byte_start=-1,
            byte_length=1,
            confidence="high",
        )


def test_signal_rejects_unknown_fields():
    """Strict mode — typos in YAML must error, not silently drop."""
    with pytest.raises(ValidationError):
        Signal(  # type: ignore[call-arg]
            name="X",
            module="Y",
            can_id=1,
            byte_start=0,
            byte_length=1,
            confidence="high",
            color="red",
        )


# ---------------------------------------------------------------------------
# decode_raw
# ---------------------------------------------------------------------------


def _sig(**kw: Any) -> Signal:
    defaults: dict[str, Any] = dict(
        name="X",
        module="ECM",
        can_id=0x100,
        byte_start=0,
        byte_length=1,
        confidence="high",
    )
    defaults.update(kw)
    return Signal(**defaults)


def test_decode_raw_single_byte():
    s = _sig(byte_start=2, byte_length=1)
    assert decode_raw(s, bytes([0x10, 0x20, 0x42, 0x99])) == 0x42


def test_decode_raw_two_byte_big_endian():
    s = _sig(byte_start=1, byte_length=2, byte_order="big")
    assert decode_raw(s, bytes([0xFF, 0x12, 0x34, 0xFF])) == 0x1234


def test_decode_raw_two_byte_little_endian():
    s = _sig(byte_start=1, byte_length=2, byte_order="little")
    assert decode_raw(s, bytes([0xFF, 0x34, 0x12, 0xFF])) == 0x1234


def test_decode_raw_returns_none_when_frame_too_short():
    s = _sig(byte_start=4, byte_length=2)
    assert decode_raw(s, bytes([0x01, 0x02])) is None


def test_decode_raw_exact_fit():
    """Signal that ends exactly at len(data) should work."""
    s = _sig(byte_start=0, byte_length=2)
    assert decode_raw(s, bytes([0x12, 0x34])) == 0x1234


# ---------------------------------------------------------------------------
# apply_scaling
# ---------------------------------------------------------------------------


def test_apply_scaling_with_scale_and_offset():
    s = _sig(scale=1.0, offset=-40.0)
    assert apply_scaling(s, 100) == 60.0


def test_apply_scaling_with_only_scale():
    s = _sig(scale=0.25)
    assert apply_scaling(s, 4) == 1.0


def test_apply_scaling_returns_none_when_scale_unset():
    s = _sig(scale=None)
    assert apply_scaling(s, 42) is None


def test_apply_scaling_treats_missing_offset_as_zero():
    s = _sig(scale=2.0, offset=None)
    assert apply_scaling(s, 5) == 10.0


def test_apply_scaling_battery_voltage_one_sixteenth():
    """The exact transform we'd expect for `BATTERY VOLT raw / 16 = volts`."""
    s = _sig(scale=1 / 16, offset=0.0)
    assert apply_scaling(s, 224) == 14.0


# ---------------------------------------------------------------------------
# is_plausible
# ---------------------------------------------------------------------------


def test_is_plausible_returns_none_when_no_bounds():
    s = _sig(plausible_min=None, plausible_max=None)
    assert is_plausible(s, 42.0) is None


def test_is_plausible_both_bounds_inside():
    s = _sig(plausible_min=0.0, plausible_max=100.0)
    assert is_plausible(s, 50.0) is True


def test_is_plausible_both_bounds_below():
    s = _sig(plausible_min=0.0, plausible_max=100.0)
    assert is_plausible(s, -5.0) is False


def test_is_plausible_both_bounds_above():
    s = _sig(plausible_min=0.0, plausible_max=100.0)
    assert is_plausible(s, 150.0) is False


def test_is_plausible_inclusive_bounds():
    """Min and max are inclusive — exactly-at-boundary counts as plausible."""
    s = _sig(plausible_min=0.0, plausible_max=100.0)
    assert is_plausible(s, 0.0) is True
    assert is_plausible(s, 100.0) is True


def test_is_plausible_min_only():
    s = _sig(plausible_min=10.0, plausible_max=None)
    assert is_plausible(s, 5.0) is False
    assert is_plausible(s, 50.0) is True


def test_is_plausible_max_only():
    s = _sig(plausible_min=None, plausible_max=10.0)
    assert is_plausible(s, 50.0) is False
    assert is_plausible(s, 5.0) is True


def test_is_plausible_coolant_minus_40_caught():
    """The actual COOLANT bug surfaced by decode_bindings.py against the
    rev session: raw=0 → decoded=-40 should be flagged as implausible."""
    s = _sig(
        scale=1.0,
        offset=-40.0,
        plausible_min=-10.0,
        plausible_max=130.0,
    )
    # raw=0 → -40 → implausible (engine-off sentinel)
    assert is_plausible(s, cast(float, apply_scaling(s, 0))) is False
    # raw=53 → 13degC → plausible (cold engine)
    assert is_plausible(s, cast(float, apply_scaling(s, 53))) is True
    # raw=130 → 90degC → plausible (thermostat-regulated)
    assert is_plausible(s, cast(float, apply_scaling(s, 130))) is True


# ---------------------------------------------------------------------------
# load_bindings
# ---------------------------------------------------------------------------


def test_load_bindings_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "bindings.yaml"
    path.write_text(
        """
schema_version: 1
vehicle:
  make: Nissan
signals:
  - name: COOLANT
    module: ECM
    can_id: 0x551
    byte_start: 0
    byte_length: 1
    scale: 1.0
    offset: -40.0
    unit: degC
    confidence: high
    evidence:
      - session: 2026-05-11T152858
        method: byte_vs_tag_differential
        finding: monotonic warmup
"""
    )
    b = load_bindings(path)
    assert b.schema_version == 1
    assert b.vehicle["make"] == "Nissan"
    assert len(b.signals) == 1
    assert b.signals[0].name == "COOLANT"
    assert b.signals[0].evidence[0].method == "byte_vs_tag_differential"


def test_load_bindings_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    path = tmp_path / "bindings.yaml"
    path.write_text(
        """
schema_version: 1
signals: []
made_up_field: hello
"""
    )
    with pytest.raises(ValidationError):
        load_bindings(path)


def test_load_actual_project_bindings():
    """The committed data/bindings.yaml must parse — it's our source of truth."""
    path = Path("data/bindings.yaml")
    if not path.exists():
        pytest.skip("data/bindings.yaml not in this checkout")
    b = load_bindings(path)
    assert b.schema_version == 1
    # We had 17 signals at the time of writing; allow growth.
    assert len(b.signals) >= 4
    # Every signal must have a name and confidence.
    for sig in b.signals:
        assert sig.name
        assert sig.confidence in {"confirmed", "high", "medium", "low", "hypothesized"}


# ---------------------------------------------------------------------------
# collect_stats (end-to-end against a synthetic session)
# ---------------------------------------------------------------------------


def _write_candump(path: Path, rows: list[tuple[float, int, bytes]]) -> None:
    """Write candump-format lines."""
    lines = []
    for ts, can_id, data in rows:
        hex_str = data.hex().upper()
        lines.append(f"({ts:.6f}) vcan0 {can_id:03X}#{hex_str}")
    path.write_text("\n".join(lines) + "\n")


def test_collect_stats_aggregates_per_signal(tmp_path: Path) -> None:
    """Synthetic session with known frames → known min/max."""
    frames = tmp_path / "frames.log"
    _write_candump(
        frames,
        [
            (1.0, 0x551, bytes([53, 0, 0, 0, 0, 0, 0])),  # cold start
            (2.0, 0x551, bytes([70, 0, 0, 0, 0, 0, 0])),  # warming
            (3.0, 0x551, bytes([90, 0, 0, 0, 0, 0, 0])),  # warm
            (1.5, 0x231, bytes([254, 0, 0, 0, 0, 0, 0, 0])),  # engine off
            (2.5, 0x231, bytes([205, 0, 0, 0, 0, 0, 0, 0])),  # engine on
        ],
    )
    signals = [
        Signal(
            name="COOLANT",
            module="ECM",
            can_id=0x551,
            byte_start=0,
            byte_length=1,
            scale=1.0,
            offset=-40.0,
            unit="degC",
            confidence="high",
        ),
        Signal(
            name="ENGINE_STATE",
            module="ECM",
            can_id=0x231,
            byte_start=0,
            byte_length=1,
            scale=1.0,
            offset=0.0,
            unit="enum",
            confidence="high",
        ),
    ]
    stats = collect_stats(frames, signals)

    c = stats["COOLANT"]
    assert c.n_samples == 3
    assert c.raw_min == 53 and c.raw_max == 90
    assert c.decoded_min == 13.0 and c.decoded_max == 50.0

    e = stats["ENGINE_STATE"]
    assert e.n_samples == 2
    assert e.raw_min == 205 and e.raw_max == 254


def test_collect_stats_handles_signal_without_scaling(tmp_path: Path) -> None:
    """Signals with scale=None should still get raw stats, no decoded."""
    frames = tmp_path / "frames.log"
    _write_candump(
        frames,
        [
            (1.0, 0x23D, bytes([0, 10, 20, 0, 0, 0, 0, 0])),
            (2.0, 0x23D, bytes([0, 50, 80, 0, 0, 0, 0, 0])),
        ],
    )
    signals = [
        Signal(
            name="REV_LEVEL",
            module="ECM",
            can_id=0x23D,
            byte_start=1,
            byte_length=2,
            scale=None,
            offset=None,
            confidence="medium",
        ),
    ]
    stats = collect_stats(frames, signals)
    r = stats["REV_LEVEL"]
    assert r.n_samples == 2
    assert r.raw_min == 0x0A14  # 10, 20 big-endian
    assert r.raw_max == 0x3250  # 50, 80 big-endian
    assert r.decoded_min is None  # scale was None
    assert r.decoded_max is None


def test_collect_stats_ignores_unrelated_frames(tmp_path: Path) -> None:
    """Frames on CAN IDs not in any signal should be skipped silently."""
    frames = tmp_path / "frames.log"
    _write_candump(
        frames,
        [
            (1.0, 0x551, bytes([60, 0, 0, 0, 0, 0, 0])),
            (1.1, 0x999, bytes([1, 2, 3])),  # unknown ID
            (1.2, 0xABC, bytes([4, 5, 6])),  # unknown ID
        ],
    )
    signals = [
        Signal(
            name="COOLANT",
            module="ECM",
            can_id=0x551,
            byte_start=0,
            byte_length=1,
            scale=1.0,
            offset=-40.0,
            confidence="high",
        ),
    ]
    stats = collect_stats(frames, signals)
    assert stats["COOLANT"].n_samples == 1


def test_collect_stats_zero_samples_for_signal_not_seen(tmp_path: Path) -> None:
    """A signal whose CAN ID never appears should have n_samples=0."""
    frames = tmp_path / "frames.log"
    _write_candump(frames, [(1.0, 0x551, bytes([60, 0]))])
    signals = [
        Signal(
            name="GHOST",
            module="ABS",
            can_id=0x123,
            byte_start=0,
            byte_length=1,
            confidence="hypothesized",
        ),
    ]
    stats = collect_stats(frames, signals)
    assert stats["GHOST"].n_samples == 0
    assert stats["GHOST"].raw_min is None
    assert stats["GHOST"].raw_max is None


def test_collect_stats_counts_implausible_samples(tmp_path: Path) -> None:
    """COOLANT with plausibility bounds: engine-off sentinel (raw=0 → -40degC)
    must be counted as implausible while real readings stay plausible."""
    frames = tmp_path / "frames.log"
    _write_candump(
        frames,
        [
            (1.0, 0x551, bytes([0, 0])),  # raw=0 → -40degC, implausible
            (1.1, 0x551, bytes([0, 0])),  # raw=0 → -40degC, implausible
            (2.0, 0x551, bytes([53, 0])),  # raw=53 → 13degC, plausible
            (3.0, 0x551, bytes([90, 0])),  # raw=90 → 50degC, plausible
        ],
    )
    signals = [
        Signal(
            name="COOLANT",
            module="ECM",
            can_id=0x551,
            byte_start=0,
            byte_length=1,
            scale=1.0,
            offset=-40.0,
            unit="degC",
            confidence="high",
            plausible_min=-10.0,
            plausible_max=130.0,
        ),
    ]
    stats = collect_stats(frames, signals)
    c = stats["COOLANT"]
    assert c.n_samples == 4
    assert c.implausible_samples == 2
    assert c.implausible_min == -40.0
    assert c.implausible_max == -40.0  # both bad samples were the same value


def test_collect_stats_no_plausibility_when_bounds_unset(tmp_path: Path) -> None:
    """A signal without plausibility bounds should have implausible_samples=0
    regardless of decoded value range."""
    frames = tmp_path / "frames.log"
    _write_candump(
        frames,
        [(1.0, 0x551, bytes([255, 0]))],  # raw=255 → 215degC if scaled
    )
    signals = [
        Signal(
            name="COOLANT",
            module="ECM",
            can_id=0x551,
            byte_start=0,
            byte_length=1,
            scale=1.0,
            offset=-40.0,
            confidence="high",
            # plausible_min/max NOT set
        ),
    ]
    stats = collect_stats(frames, signals)
    assert stats["COOLANT"].implausible_samples == 0


def test_collect_stats_implausible_skipped_when_no_scaling(tmp_path: Path) -> None:
    """Plausibility lives in decoded space; signals without scaling can't
    have decoded values, so implausible count stays 0 even if bounds are set."""
    frames = tmp_path / "frames.log"
    _write_candump(frames, [(1.0, 0x100, bytes([200]))])
    signals = [
        Signal(
            name="UNSCALED",
            module="X",
            can_id=0x100,
            byte_start=0,
            byte_length=1,
            scale=None,
            confidence="hypothesized",
            plausible_min=0.0,
            plausible_max=10.0,  # set but no decoded value
        ),
    ]
    stats = collect_stats(frames, signals)
    s = stats["UNSCALED"]
    assert s.n_samples == 1
    assert s.implausible_samples == 0  # can't check without decoded value


# ---------------------------------------------------------------------------
# A04.6 — sentinels on broadcast bindings
# ---------------------------------------------------------------------------


def _tire(**extra: object) -> dict[str, object]:
    return {
        "name": "TIRE_A",
        "module": "BCM",
        "can_id": 0x385,
        "byte_start": 2,
        "byte_length": 1,
        "scale": 0.25,
        "unit": "psi",
        "confidence": "hypothesized",
        **extra,
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"sentinel_raw": 0},
        {"sentinel_meaning": "asleep"},
        {"sentinel_raw": 0, "sentinel_meaning": "  "},
        {"sentinel_raw": 256, "sentinel_meaning": "cannot occur in one byte"},
        {"sentinel_raw": -1, "sentinel_meaning": "cannot occur unsigned"},
    ],
)
def test_a_half_declared_or_impossible_sentinel_is_refused(extra: dict[str, object]) -> None:
    from pydantic import ValidationError

    from src.decode.bindings import Signal

    with pytest.raises(ValidationError, match="sentinel"):
        Signal.model_validate(_tire(**extra))


def test_a_sentinel_scales_to_nothing_everywhere() -> None:
    import pandas as pd

    from src.decode.bindings import Bindings, Signal, apply_scaling
    from src.decode.engineering import physical_series, physical_value

    signal = Signal.model_validate(_tire(sentinel_raw=0, sentinel_meaning="asleep"))
    catalog = Bindings(schema_version=1, signals=[signal])
    assert apply_scaling(signal, 0) is None
    assert apply_scaling(signal, 148) == 37.0
    assert physical_value(0.0, "decoded:TIRE_A", catalog) is None
    assert physical_value(148.0, "decoded:TIRE_A", catalog) == 37.0
    series = physical_series(pd.Series([0.0, 148.0]), "decoded:TIRE_A", catalog)
    assert series.isna().tolist() == [True, False] and series.iloc[1] == 37.0

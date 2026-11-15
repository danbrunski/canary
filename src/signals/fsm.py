"""Physical-impossibility bounds for decoded channels.

Serve-time module: the quality gate, the plausibility layer, the derived
store and the ROM editor read ``PHYSICAL_LIMITS`` from here, so this module
imports nothing but the standard library. The FSM PDF scraper (positional
extraction of CONSULT-III parameter names and spec ranges, which needs the
dev-only ``pdfplumber``) lives in :mod:`src.signals.fsm_extract`;
``tests/test_runtime_dependencies.py`` asserts that nothing the daemon
imports reaches it.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Physical (decode-level) limits — online-training-strategy.md Task 1.1
# ---------------------------------------------------------------------------
#
# "Physical limits" are a different axis than the FSM "expected ranges"
# (:mod:`src.signals.fsm_extract`).
# An expected range is *spec normal* (what a healthy engine reads); a
# physical limit is *decode-level impossibility* (sensor-open, sensor-short,
# a 16-bit rollover) — garbage, not an unhealthy reading. Mixing the two
# is exactly the trap online-training-strategy.md warns about: filtering
# training data to "expected" ranges throws away legitimate but abnormal
# engine states, whereas physical limits only ever discard decode noise.
#
# Primary source would be the FSM itself, if its CONSULT-III data-monitor
# tables spelled out sensor-fault codes in the spec text (e.g. "Sensor
# open: -40°C"). ``fsm_extract.extract_physical_limits_from_fsm`` looks for that
# vocabulary and is exercised by its own tests, but a scan of the real
# ``data/fsm/signals.json`` corpus turns up nothing — the Nissan tables only
# ever describe nominal operating conditions, never decode-fault codes, so
# it always returns an empty dict on real data. ``PHYSICAL_LIMITS`` is
# therefore the actual source of truth: a hand-curated table keyed by the
# same channel names the preprocessing pipeline uses (``obd:RPM``, etc.),
# seeded from the OBD-II decode formulas in
# ``src/collection/can_obd_client.py`` — a bound is *only* added when the
# decode formula (or basic physics) makes a value outside it impossible,
# never a guess at a "typical" range. A channel absent from this table has
# no physical-limit reject — that is the safe default, not an oversight.


@dataclass(frozen=True)
class PhysicalLimit:
    """One channel's absolute physical min/max — decode-level "impossible"
    bounds, not the FSM spec's nominal operating range.

    Same ``{min, max, unit}`` shape as :class:`~src.analysis.signal_lookup.
    ExpectedRange` (``float("-inf")``/``float("inf")`` for a one-sided bound)
    so ``quality_gate.py`` and ``range_match.py`` share one schema. ``reason``
    is why the bound is safe to reject on (cites the decode formula or the
    physical fact), surfaced in the disposition report so a reject is never
    an unexplained number.
    """

    min: float
    max: float
    unit: str
    reason: str


#: Curated physical-impossibility bounds, keyed by the wide-frame channel
#: name (``<source>:<CHANNEL>``, matching ``src.telemetry.channel_key``).
#: Every bound below traces to a decode formula in
#: ``src/collection/can_obd_client.py`` (or basic physics) — see each entry's
#: ``reason``. Channels not listed here are deliberately left out: either the
#: decode formula alone can't produce an out-of-range value (most single-byte
#: percent PIDs), or the "physically impossible" bound would require
#: vehicle-specific knowledge (electrical system voltage, redline) this
#: module has no principled way to guess — an absent entry means "no
#: reject", which is the safe default.
PHYSICAL_LIMITS: dict[str, PhysicalLimit] = {
    "obd:RPM": PhysicalLimit(
        min=0.0,
        max=10_000.0,
        unit="rpm",
        reason=(
            "PID 0x0C decodes (A*256+B)/4, ceiling 16383.75 at raw 0xFFFF — a "
            "classic all-ones sensor-fault code, not a real engine speed; no "
            "production gasoline engine sustains anywhere near this either"
        ),
    ),
    "obd:COOLANT_TEMP": PhysicalLimit(
        min=-39.0,
        max=150.0,
        unit="degC",
        reason=(
            "PID 0x05 decodes A-40; raw A=0 -> exactly -40degC is the sensor-"
            "open floor, not a real coolant reading. >150degC is beyond "
            "boiling/failure, still short of the 215degC encoding ceiling"
        ),
    ),
    "obd:INTAKE_TEMP": PhysicalLimit(
        min=float("-inf"),
        max=250.0,
        unit="degC",
        reason=(
            "intake air can legitimately read very cold (cold-climate "
            "startup), so no lower bound; >=250degC is decode/sensor-fault "
            "territory for intake air on any port/direct-injection engine"
        ),
    ),
    "obd:MAF": PhysicalLimit(
        min=0.0,
        max=float("inf"),
        unit="g/s",
        reason="PID 0x10 decodes (A*256+B)/100, a magnitude — negative is impossible",
    ),
    "obd:THROTTLE_POS": PhysicalLimit(
        min=0.0,
        max=100.0,
        unit="%",
        reason="PID 0x11 decodes A*100/255 (single byte); formula cannot exceed 0-100%",
    ),
    "obd:RELATIVE_THROTTLE_POS": PhysicalLimit(
        min=0.0,
        max=100.0,
        unit="%",
        reason="PID 0x45, same A*100/255 single-byte formula as THROTTLE_POS",
    ),
    "obd:ENGINE_LOAD": PhysicalLimit(
        min=0.0,
        max=100.0,
        unit="%",
        reason="PID 0x04, same A*100/255 single-byte formula as THROTTLE_POS",
    ),
    "obd:FUEL_LEVEL": PhysicalLimit(
        min=0.0,
        max=100.0,
        unit="%",
        reason="PID 0x2F, same A*100/255 single-byte formula as THROTTLE_POS",
    ),
    "obd:SHORT_FUEL_TRIM_1": PhysicalLimit(
        min=-100.0,
        max=100.0,
        unit="%",
        reason="PID 0x06 decodes (A-128)*100/128; formula bounds -100%..+99.2%",
    ),
    "obd:SHORT_FUEL_TRIM_2": PhysicalLimit(
        min=-100.0,
        max=100.0,
        unit="%",
        reason="PID 0x08, same signed-byte formula as SHORT_FUEL_TRIM_1",
    ),
    "obd:LONG_FUEL_TRIM_1": PhysicalLimit(
        min=-100.0,
        max=100.0,
        unit="%",
        reason="PID 0x07, same signed-byte formula as SHORT_FUEL_TRIM_1",
    ),
    "obd:LONG_FUEL_TRIM_2": PhysicalLimit(
        min=-100.0,
        max=100.0,
        unit="%",
        reason="PID 0x09, same signed-byte formula as SHORT_FUEL_TRIM_1",
    ),
    "obd:BAROMETRIC_PRESSURE": PhysicalLimit(
        min=50.0,
        max=110.0,
        unit="kPa",
        reason=(
            "PID 0x33 decodes the raw byte directly (0-255 kPa possible), but "
            "Earth's terrestrial atmospheric pressure never leaves roughly "
            "30-108 kPa; loose margin kept either side of that"
        ),
    ),
}

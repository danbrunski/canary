"""One byte-slice / linear-scale / plausible-range layer for every decode (R05.9).

A broadcast binding (``bindings.yaml``), a Mode 22 DID and a service-0x23 RAM
channel (``dids.yaml``) and an add-on sensor frame (``sensor_can_signals.yaml``)
all describe a value the same way: *these bytes, this order, times a scale
plus an offset, and it should land in this range*. Each catalog grew its own
copy of that arithmetic, and the copies drifted — only the sensor decoder
understood ``signed``, and the two ``Evidence`` models disagreed about whether
a bare constant may be recorded.

This module is the arithmetic, once:

- :func:`slice_raw`, :func:`linear_factors` / :func:`linear_value` and
  :func:`in_range` are plain functions over the numbers, for catalogs whose
  field names differ (the sensor catalog says ``endianness``/``valid_min``);
- :class:`ByteSlice`, :class:`LinearScale` and :class:`PlausibleRange` are
  method-only mixins over the conventional field names, so a model gains the
  behaviour without the mixin reordering or re-declaring its fields;
- :class:`Evidence` is the one evidence record, with the refusal that a
  ``constant`` never travels without its ``conditions`` and unit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, model_validator

ByteOrder = Literal["big", "little"]


def slice_raw(
    data: bytes,
    byte_start: int,
    byte_length: int,
    byte_order: ByteOrder,
    *,
    signed: bool = False,
) -> int | None:
    """The integer at ``data[byte_start : byte_start + byte_length]``.

    ``None`` when ``data`` is too short for the slice — a frame whose DLC
    varies, or a response shorter than the catalog declares.
    """
    end = byte_start + byte_length
    if len(data) < end:
        return None
    return int.from_bytes(data[byte_start:end], byteorder=byte_order, signed=signed)


def linear_factors(scale: float | None, offset: float | None) -> tuple[float, float] | None:
    """``(scale, offset)``, or ``None`` while the scale is unpinned.

    An unset offset means zero. An unpinned scale has no engineering value:
    the raw count is still useful for variability, never as "23.5 degC".
    """
    if scale is None:
        return None
    return scale, (offset if offset is not None else 0.0)


def linear_value(raw: float, scale: float | None, offset: float | None) -> float | None:
    """``raw * scale + offset``, or ``None`` while the scale is unpinned."""
    factors = linear_factors(scale, offset)
    if factors is None:
        return None
    factor, shift = factors
    return raw * factor + shift


def in_range(value: float, lower: float | None, upper: float | None) -> bool | None:
    """Whether ``value`` is inside ``[lower, upper]`` (inclusive).

    ``None`` when neither bound is set (no check). Either bound alone is a
    one-sided check.
    """
    if lower is None and upper is None:
        return None
    if lower is not None and value < lower:
        return False
    if upper is not None and value > upper:
        return False
    return True


class ByteSlice:
    """Mixin: decode the raw integer from ``byte_start``/``byte_length``/``byte_order``."""

    # Declared for type checking only: a runtime annotation here would make
    # pydantic collect these as fields of the model and reorder its schema.
    if TYPE_CHECKING:
        byte_start: int
        byte_length: int
        byte_order: ByteOrder

    def raw_from(self, data: bytes) -> int | None:
        return slice_raw(data, self.byte_start, self.byte_length, self.byte_order)


class LinearScale:
    """Mixin: the ``scale``/``offset`` linear map."""

    if TYPE_CHECKING:
        scale: float | None
        offset: float | None

    def scaling_factors(self) -> tuple[float, float] | None:
        return linear_factors(self.scale, self.offset)

    def scaled(self, raw: float) -> float | None:
        return linear_value(raw, self.scale, self.offset)


class PlausibleRange:
    """Mixin: the ``plausible_min``/``plausible_max`` sanity bounds."""

    if TYPE_CHECKING:
        plausible_min: float | None
        plausible_max: float | None

    def plausible(self, value: float) -> bool | None:
        return in_range(value, self.plausible_min, self.plausible_max)


class Evidence(BaseModel):
    """One piece of evidence backing a binding, DID or channel identification.

    Evidence that establishes a *number* must also say when the number holds.
    A calibration measured at steady state does not apply during a transient,
    and one measured in closed loop does not apply under enrichment — quoting
    a constant outside its conditions is the shape of every retracted finding
    in this project's history (AGENTS.md, "never compare aggregates"). So
    ``constant`` cannot be recorded without ``conditions``: the validator
    below makes the unsafe entry unwritable rather than merely discouraged.
    """

    model_config = ConfigDict(extra="forbid")

    session: str
    method: str
    finding: str
    #: The quantitative result, when this evidence establishes one.
    constant: float | None = None
    #: Units of ``constant``, e.g. ``"counts per gram of fuel"``.
    constant_unit: str | None = None
    #: The operating envelope the constant was measured in, and outside which
    #: it must not be quoted.
    conditions: str | None = None

    @model_validator(mode="after")
    def _constant_requires_conditions_and_unit(self) -> Evidence:
        if self.constant is None:
            return self
        missing = [
            name
            for name, value in (
                ("conditions", self.conditions),
                ("constant_unit", self.constant_unit),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"evidence carrying `constant` must also set {' and '.join(missing)} "
                "— a bare number invites being quoted against data it was never "
                "measured on"
            )
        return self


__all__ = [
    "ByteOrder",
    "ByteSlice",
    "Evidence",
    "LinearScale",
    "PlausibleRange",
    "in_range",
    "linear_factors",
    "linear_value",
    "slice_raw",
]

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
  :func:`slice_raw_column` is :func:`slice_raw` over a whole column of
  payloads at once (the session decode on read, P02.8), tested value for
  value against it;
- :class:`ByteSlice`, :class:`LinearScale` and :class:`PlausibleRange` are
  method-only mixins over the conventional field names, so a model gains the
  behaviour without the mixin reordering or re-declaring its fields;
- :class:`Evidence` is the one evidence record, with the refusal that a
  ``constant`` never travels without its ``conditions`` and unit;
- :class:`SentinelState` and :func:`check_sentinel` are the "a sentinel is a
  state, not a number" rule (AGENTS.md): a declared raw word that means *no
  measurement* decodes to a ``<NAME>_SENTINEL`` state channel carrying its
  stated meaning, never to a value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

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


def payload_lengths(payloads: Any) -> Any:
    """Each payload's byte length (an ``int64`` array), ``-1`` for anything
    that is not ``bytes``/``bytearray`` — which :func:`slice_raw_column`
    treats as no payload, as a scalar caller skipping it does.

    A column of ``bytes`` and nulls (what Parquet gives) is measured by Arrow
    without a Python call per payload; anything else element by element.
    """
    import numpy as np
    import pandas as pd
    import pyarrow as pa
    import pyarrow.compute as pc

    if len(payloads) and pd.api.types.infer_dtype(payloads, skipna=True) == "bytes":
        column = pa.array(payloads, type=pa.binary(), from_pandas=True)
        measured = pc.fill_null(pc.binary_length(column), -1)  # type: ignore[attr-defined, no-untyped-call, unused-ignore]
        return np.asarray(measured.to_numpy(), dtype=np.int64)
    return np.fromiter(
        (len(p) if isinstance(p, (bytes, bytearray)) else -1 for p in payloads),
        dtype=np.int64,
        count=len(payloads),
    )


def slice_raw_column(
    payloads: Any,
    lengths: Any,
    byte_start: int,
    byte_length: int,
    byte_order: ByteOrder,
) -> Any:
    """:func:`slice_raw` for a column of payloads, as ``float64``; NaN where
    :func:`slice_raw` would give ``None`` or there is no payload.

    ``payloads`` is an object array, ``lengths`` its :func:`payload_lengths`
    (computed once per column and shared by every slice of it). Payloads are
    grouped by length and each group is sliced as one ``uint8`` matrix, so a
    frame costs no Python call. Unsigned only, at most 8 bytes: ``uint64``
    holds any such slice exactly, and its ``float64`` rounds as ``float(int)``
    does. (No catalog that decodes a column declares a signed slice.)
    """
    import numpy as np

    if not 1 <= byte_length <= 8:
        raise ValueError(f"a column slice is 1-8 bytes, not {byte_length}")
    out = np.full(len(payloads), np.nan, dtype=np.float64)
    end = byte_start + byte_length
    for length in np.unique(lengths[lengths >= end]):
        rows = np.flatnonzero(lengths == length)
        matrix = np.frombuffer(b"".join(payloads[rows]), dtype=np.uint8).reshape(
            rows.size, int(length)
        )
        window = matrix[:, byte_start:end].astype(np.uint64)
        if byte_order == "little":
            window = window[:, ::-1]
        raw = np.zeros(rows.size, dtype=np.uint64)
        for column in range(byte_length):
            raw = (raw << np.uint64(8)) | window[:, column]
        out[rows] = raw.astype(np.float64)
    return out


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


#: Suffix of the state channel a sentinel decodes to (``mem:<NAME>_SENTINEL``,
#: ``decoded:<NAME>_SENTINEL``).
SENTINEL_SUFFIX = "_SENTINEL"


def sentinel_channel(name: str) -> str:
    """The state channel ``name``'s sentinel decodes to."""
    return f"{name}{SENTINEL_SUFFIX}"


def check_sentinel(
    name: str,
    sentinel_raw: int | None,
    sentinel_meaning: str | None,
    *,
    byte_length: int,
    signed: bool = False,
) -> None:
    """Refuse a half-declared or unrepresentable sentinel.

    Raw and meaning travel together: a sentinel without a stated meaning
    decodes as a measurement, and a meaning without a raw word matches
    nothing. The raw word must fit the slice, or it can never match and the
    declaration is silently dead.
    """
    if (sentinel_raw is None) != (sentinel_meaning is None or not sentinel_meaning.strip()):
        raise ValueError(
            f"{name}: sentinel_raw and sentinel_meaning must be set together "
            "-- a sentinel without a stated meaning decodes as a measurement"
        )
    if sentinel_raw is None:
        return
    bits = 8 * byte_length
    low, high = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    if not low <= sentinel_raw <= high:
        raise ValueError(
            f"{name}: sentinel_raw {sentinel_raw} cannot occur in a {byte_length}-byte "
            f"{'signed' if signed else 'unsigned'} slice ({low}..{high})"
        )


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


class SentinelState:
    """Mixin: the ``sentinel_raw``/``sentinel_meaning`` no-measurement state."""

    if TYPE_CHECKING:
        sentinel_raw: int | None
        sentinel_meaning: str | None

    def is_sentinel(self, raw: float) -> bool:
        """Whether ``raw`` is the declared no-measurement word."""
        return self.sentinel_raw is not None and raw == self.sentinel_raw


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
    "SENTINEL_SUFFIX",
    "SentinelState",
    "check_sentinel",
    "in_range",
    "linear_factors",
    "linear_value",
    "payload_lengths",
    "sentinel_channel",
    "slice_raw",
    "slice_raw_column",
]

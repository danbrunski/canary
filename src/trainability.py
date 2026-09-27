"""Which wide-frame channels may train the anomaly detectors, by source (H01.1).

One function, :func:`source_exclusion_reason`, answers "does this channel's
*origin* allow it to train?" for every consumer that chooses features or model
inputs: the feature pipeline's :class:`~src.preprocessing.feature_pipeline.FeaturePolicy`
(Isolation Forest and sequence detector), the sequence feature-set role tables,
and behavior-model recipes and artifacts. It is an allowlist:

* a :data:`~src.telemetry.TelemetrySource` trains only when listed in
  :data:`~src.telemetry.TRAINABLE_SOURCES` (``obd``, ``decoded``);
* a wide-column-only namespace trains only when listed in
  :data:`TRAINABLE_WIDE_NAMESPACES`;
* a ``derived:`` channel trains only when every input declared for it in
  :data:`src.signals.derived.DERIVED_CHANNEL_INPUTS` (and
  ``DERIVED_CHANNEL_OPTIONAL_INPUTS``) does — an undeclared one does not;
* a committee channel — a ``derived:<NAME>`` spread or a member transform of
  :mod:`src.signals.committees` — never does, whatever its inputs: committees
  are detectors, not features (H04.1). This holds even for
  ``derived:OBD_SPEED_ACCEL_G``, whose inputs are all ECU data;
* anything else (an unclassified namespace, a bare unprefixed name) does not.

External sensors (``gps``, ``imu``, ``sensor``, the HAT's 12 V and barometer
channels) never train or score the detectors: H00 operator decision 1
(2026-09-23). They take part in detection only through committees.

This is the **source** gate, over channels. Whether a *capture profile's*
sessions may be trained, calibrated, evaluated or scored on (tuning captures:
none of these, operator decision 2026-09-24, R03.12) is a separate,
profile-level gate over sessions. Passing this gate never makes a tuning
capture eligible.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Final, cast

from src.columns import base_channel
from src.telemetry import (
    DBC_SOURCE,
    INPUT_GATED_SOURCES,
    NON_TRAINABLE_SOURCES,
    TRAINABLE_SOURCES,
    source_of,
)

#: The stable name every refusal from this gate starts with. Surfaced in
#: manifest ``feature_policy_exclusions``, ``/status`` load audits and
#: behavior-model load errors.
SOURCE_NOT_TRAINABLE: Final = "source_not_trainable"

#: Namespaces that exist only as wide-column prefixes (never a
#: ``TelemetryRecord.source``) and may train, each with why. Anything absent is
#: not trainable.
TRAINABLE_WIDE_NAMESPACES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "did": (
            "Mode 22 DIDs read from the ECU by the OBD poller; further gated on "
            "the serving capture profile polling them"
        ),
        DBC_SOURCE: ("broadcast CAN decoded through a DBC; further gated on the bindings catalog"),
        "deviation": (
            "behavior-model residuals; the models' own inputs are refused at load unless trainable"
        ),
    }
)

#: ``NON_TRAINABLE_SOURCES`` keyed for lookup by an arbitrary column prefix.
_NON_TRAINABLE: Final = cast(Mapping[str, str], NON_TRAINABLE_SOURCES)


def _base(column: str) -> str:
    return base_channel(column)


def source_exclusion_reason(column: str) -> str | None:
    """Why ``column``'s origin keeps it out of every trained model, or ``None``.

    Rolling expansions (``__mean``/``__std``/``__roc``) follow their base
    channel. The reason always starts with :data:`SOURCE_NOT_TRAINABLE`.
    """
    detail = _detail(_base(column), frozenset())
    return None if detail is None else f"{SOURCE_NOT_TRAINABLE}: {detail}"


def _detail(base: str, seen: frozenset[str]) -> str | None:
    source = source_of(base)
    if source is None:
        return f"{base!r} names no source namespace"
    if source in TRAINABLE_SOURCES or source in TRAINABLE_WIDE_NAMESPACES:
        return None
    if source in _NON_TRAINABLE:
        return f"{source}: {_NON_TRAINABLE[source]}"
    if source in INPUT_GATED_SOURCES:
        return _derived_detail(base, seen)
    return f"source {source!r} is not classified as trainable (src.telemetry.TRAINABLE_SOURCES)"


def _derived_detail(channel: str, seen: frozenset[str]) -> str | None:
    # Lazy: src.signals.derived pulls pandas and the decode catalog, and this
    # module is imported by the behavior-model schema.
    from src.signals.committees import committee_channel_reason
    from src.signals.derived import DERIVED_CHANNEL_INPUTS, DERIVED_CHANNEL_OPTIONAL_INPUTS

    committee = committee_channel_reason(channel)
    if committee is not None:
        return committee

    groups = DERIVED_CHANNEL_INPUTS.get(channel)
    if groups is None:
        return (
            f"{channel} declares no inputs (src.signals.derived.DERIVED_CHANNEL_INPUTS); "
            "an undeclared derived channel is not trainable"
        )
    if channel in seen:
        return f"{channel} depends on itself"
    nested = seen | {channel}
    inputs = [column for group in groups for column in group]
    inputs.extend(DERIVED_CHANNEL_OPTIONAL_INPUTS.get(channel, ()))
    for column in dict.fromkeys(inputs):
        detail = _detail(column, nested)
        if detail is not None:
            return f"{channel} is computed from {column} ({detail})"
    return None


def non_trainable_inputs(columns: Iterable[str]) -> dict[str, str]:
    """``{column: reason}`` for each of ``columns`` this gate refuses."""
    out: dict[str, str] = {}
    for column in columns:
        reason = source_exclusion_reason(str(column))
        if reason is not None:
            out[str(column)] = reason
    return out


__all__ = [
    "SOURCE_NOT_TRAINABLE",
    "TRAINABLE_WIDE_NAMESPACES",
    "non_trainable_inputs",
    "source_exclusion_reason",
]

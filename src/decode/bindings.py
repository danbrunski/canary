"""Signal bindings loader + decoder.

Reads `data/bindings.yaml` — the project's canonical record of "byte position
X in CAN ID Y means Z" — into typed Signal objects, then provides the two
functions the daemon (and analyzers) need to actually use them at runtime:

  * `decode_raw(signal, data)` — pull the raw integer out of a frame
  * `apply_scaling(signal, raw)` — turn it into an engineering value

The two are separate because some signals (motion candidates, hypothesized
entries) have no scaling yet; the raw extraction still works, the scaling
returns None until we pin scale/offset against ground truth.

Strict pydantic schema so typos in the YAML fail loudly, not silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Strict):
    """One piece of evidence backing a signal binding."""

    session: str
    method: str
    finding: str


class Signal(_Strict):
    """A signal binding: where in a CAN frame the value lives + how to decode it.

    `byte_order` is "big" for MSB-first (the OBD-II/UDS convention used across
    most of Nissan's broadcast frames we've seen so far). Set explicitly to
    "little" if a signal turns out to be little-endian.

    `scale` and `offset` are `None` for signals where we know the byte
    position but haven't yet pinned the engineering scaling — `apply_scaling`
    returns None in that case. Promoting a signal to known scale/offset is
    typically done after ELM327 Mode 01 cross-reference.
    """

    name: str
    module: str
    can_id: int = Field(ge=0, le=0x1FFFFFFF)  # accommodates 29-bit extended IDs
    byte_start: int = Field(ge=0, le=63)
    byte_length: int = Field(ge=1, le=8)
    byte_order: Literal["big", "little"] = "big"
    scale: float | None = None
    offset: float | None = None
    unit: str = "unknown"
    confidence: Literal["confirmed", "high", "medium", "low", "hypothesized"]
    #: Persist this signal as a ``decoded:`` row regardless of confidence.
    #:
    #: Deliberately orthogonal to ``confidence``, because they answer different
    #: questions. Confidence is *is this binding trustworthy*; this is *is it
    #: worth disk forever*, and the answers diverge in both directions. A
    #: `confirmed` third copy of a value already persisted from another frame
    #: earns nothing at 88 Hz, while a `high` wheel speed is the only fast,
    #: always-present road-speed source the live segmenter can reach.
    #:
    #: Persisting buys serve-time reach only — never information. Raw frame
    #: payloads are stored regardless, and the training pipeline re-decodes
    #: every binding from them irrespective of confidence or this flag. Set it
    #: only when something *live* (segmenter, dashboard, status) needs the
    #: value without a payload decode.
    persist: bool = False
    #: May this signal be a *trained model feature* (IF / sequence detector)?
    #:
    #: Orthogonal to ``confidence`` the same way ``persist`` is. Confidence
    #: answers *is this byte what we say it is*; this answers *is it engine
    #: behaviour*. A `high` brake-pressure byte is a perfectly good binding and
    #: a useless anomaly feature: the detectors model how the powertrain
    #: behaves, and brake, steering, HVAC-blower, TPMS and body channels only
    #: teach them what the driver did. The 2026-09-20 retrain carried all of
    #: them: 173-255 IF features on a few hundred rows per mode, in a model
    #: whose fault-injection recall came out at 4-16 %.
    #:
    #: A vehicle fact, so it lives here in the data file — shared code only
    #: reads the flag (:func:`is_trainable_binding`). It does NOT affect
    #: decoding, persistence, the DBC emit, the dashboard, or analysis: the
    #: channel stays in every frame, it is just not handed to a model.
    model_feature: bool = True
    # Optional sanity bounds for the *decoded* value. When set, the decoder
    # flags samples that fall outside this range — typical use is to catch
    # binding bugs (e.g. coolant decoding to -40degC because the scale/offset
    # was misapplied to a "no reading" sentinel). Leave unset for signals
    # without scaling or signals whose plausible range isn't yet known.
    plausible_min: float | None = None
    plausible_max: float | None = None
    #: The binding that replaced this one, named explicitly.
    #:
    #: A superseded binding stays here because stored telemetry and older
    #: model manifests still reference its name, but it is no longer the
    #: right way to read the quantity. Two bindings that describe the same
    #: bytes then *overlap* by construction, which is a contradiction to
    #: anything consuming the geometry — the DBC emit refused the whole
    #: file when ``MOTION_SIGNAL_2D1_1`` and ``VEHICLE_SPEED_2D1`` both
    #: claimed byte 1 of 0x2D1.
    #:
    #: Recording the relationship makes that resolvable without guessing:
    #: consumers that need one reading per quantity skip superseded
    #: bindings, and an overlap between two *live* bindings stays the error
    #: it should be. The name is validated against the file, so a rename or
    #: a typo cannot leave a dangling pointer.
    superseded_by: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    notes: str | None = None


class Bindings(_Strict):
    """Top-level bindings.yaml document."""

    schema_version: int
    vehicle: dict[str, Any] = Field(default_factory=dict)
    bus: dict[str, Any] | None = None
    signals: list[Signal]
    # Quantities SEARCHED FOR on the broadcast bus and not found, with the
    # dynamic range the search actually covered. Recorded so a future scan does
    # not re-derive the same negative, and so "we never looked" stays
    # distinguishable from "we looked with a well-powered sweep and it is not
    # there". Not consumed by the decoder.
    absent_signals: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _check_superseded_targets(self) -> Bindings:
        """Every ``superseded_by`` must name a signal in this file."""
        names = {signal.name for signal in self.signals}
        dangling = {
            signal.name: signal.superseded_by
            for signal in self.signals
            if signal.superseded_by is not None and signal.superseded_by not in names
        }
        if dangling:
            rendered = ", ".join(f"{k} -> {v}" for k, v in sorted(dangling.items()))
            raise ValueError(f"superseded_by names no such signal: {rendered}")
        return self


#: Binding confidences a model may train on. `medium` and below are working
#: hypotheses about a byte — evidence for the reverse-engineering notebook,
#: not for a detector. Six features were corrected out of bindings.yaml after
#: the 2026-07 IF trained on them and the model went blind at serve
#: (`project_if_blind_at_serve_2026-09`): a binding that can still be retracted
#: must not be able to become a trained feature in the first place.
TRAINABLE_CONFIDENCES: frozenset[str] = frozenset({"confirmed", "high"})


def is_trainable_binding(signal: Signal) -> bool:
    """``True`` when ``signal`` may be a trained model feature.

    Both gates: the binding is trustworthy (:data:`TRAINABLE_CONFIDENCES`) AND
    the data file has not opted it out as non-engine behaviour
    (``model_feature: false``).
    """
    return signal.confidence in TRAINABLE_CONFIDENCES and signal.model_feature


def load_bindings(path: Path) -> Bindings:
    """Load and validate a bindings.yaml file. Raises ValidationError on schema mismatch."""
    raw = yaml.safe_load(path.read_text())
    return Bindings.model_validate(raw)


def decode_raw(signal: Signal, data: bytes) -> int | None:
    """Extract the raw integer value of `signal` from one frame's data bytes.

    Returns `None` when the frame is too short to contain the signal — that
    happens when an ID's DLC varies across frames, or when a signal's byte
    range overshoots the canonical DLC for its CAN ID.
    """
    end = signal.byte_start + signal.byte_length
    if len(data) < end:
        return None
    chunk = data[signal.byte_start : end]
    return int.from_bytes(chunk, byteorder=signal.byte_order)


def scaling_factors(signal: Signal) -> tuple[float, float] | None:
    """``(scale, offset)`` for `signal`, or `None` when `scale` is unpinned.

    The one place the binding's linear map is read; :func:`apply_scaling` and
    the vectorized :mod:`src.decode.engineering` accessor both go through it,
    so a raw column and a raw scalar can never be scaled two different ways.
    An unset `offset` means zero.
    """
    if signal.scale is None:
        return None
    return signal.scale, (signal.offset if signal.offset is not None else 0.0)


def apply_scaling(signal: Signal, raw: float) -> float | None:
    """Apply `scale` and `offset` to a raw value.

    Returns `None` when `scale` is unset — the binding's scaling hasn't been
    pinned to ground truth yet, so we can't emit an engineering value. The
    raw extraction is still useful for variability tracking, just not for
    "this signal reads 23.5 °C".
    """
    factors = scaling_factors(signal)
    if factors is None:
        return None
    scale, offset = factors
    return raw * scale + offset


def is_plausible(signal: Signal, decoded: float) -> bool | None:
    """Check whether `decoded` falls inside the signal's plausible range.

    Returns:
      * `True`  — decoded is within `[plausible_min, plausible_max]` inclusive
      * `False` — decoded is outside the bounds
      * `None`  — at least one bound is unset; no check performed

    Either bound can be omitted: a min-only bound means "must be at least X",
    a max-only bound means "must be at most X". Only fully-unset signals
    skip the check.
    """
    if signal.plausible_min is None and signal.plausible_max is None:
        return None
    if signal.plausible_min is not None and decoded < signal.plausible_min:
        return False
    if signal.plausible_max is not None and decoded > signal.plausible_max:
        return False
    return True

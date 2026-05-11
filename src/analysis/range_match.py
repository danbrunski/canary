"""Bind captured Mode 22 DIDs to FSM parameters by value-range fitting.

The premise: every FSM CONSULT-III parameter has expected numeric ranges
(e.g. `BATTERY VOLT 11-14 V`) and every captured DID has an observed raw
range (e.g. `0x1204 raw 210-237`). For each (DID, FSM_param) pair, we
sweep a small grid of common scalings (1, 1/2, 1/4, ..., 1/256, 1/1000)
and offsets (0, -40, -128) — looking for a transform that maps the raw
range into the FSM-expected range. A clean fit IS the binding, with
the scale/offset fixed as a byproduct.

This is the third leg of the binding tripod, alongside:
  * Module attribution (CAN ID → ECM/ABS/...)
  * Tag-fuzzy + range-expansion (per-window analysis in the decoder)

The first two narrow the candidate set; range-match scores the survivors.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from math import isfinite

from src.analysis.signal_lookup import ExpectedRange, ParamRef

# Common Mode 22 scaling denominators across automotive ECUs. Inverses
# (i.e. `× 0.001` rather than `÷ 1000`) so a single multiply gives the
# decoded value: decoded = raw * scale + offset.
_DEFAULT_SCALINGS: tuple[float, ...] = (
    1.0,
    1 / 2,
    1 / 4,
    1 / 8,
    1 / 10,
    1 / 16,
    1 / 32,
    1 / 64,
    1 / 100,
    1 / 128,
    1 / 256,
    1 / 1000,
    # Some Nissan signals scale by /256 + multiplied unit (e.g. RPM /4 raw,
    # but other manufacturers use 0.25 * raw — equivalent here as 1/4).
)
# Common offsets for OBD-II encodings: 0 (most signals), -40 (temperature
# in many vehicles), -128 (signed-byte values like timing advance).
_DEFAULT_OFFSETS: tuple[float, ...] = (0.0, -40.0, -128.0)


@dataclass(frozen=True)
class RangeMatch:
    """One scored candidate FSM binding for a captured DID.

    `score` is in [0, 1]; higher is better. Computed from how well the
    decoded midpoint sits inside the expected range AND how close the
    decoded span is to the expected span.
    """

    param: ParamRef
    expected: ExpectedRange
    scale: float
    offset: float
    decoded_min: float
    decoded_max: float
    score: float


def _score_bounded(
    raw_lo: float, raw_hi: float, exp_lo: float, exp_hi: float, scale: float, offset: float
) -> float:
    """Score a fully-bounded expected range."""
    dec_lo = raw_lo * scale + offset
    dec_hi = raw_hi * scale + offset
    dec_mid = (dec_lo + dec_hi) / 2
    exp_mid = (exp_lo + exp_hi) / 2
    exp_span = exp_hi - exp_lo
    if exp_span <= 0:
        return 0.0
    # Hard requirement: decoded midpoint must be inside the expected band
    # (with a tiny outside-tolerance for hunting at the edge of idle).
    tolerance = exp_span * 0.1
    if not (exp_lo - tolerance <= dec_mid <= exp_hi + tolerance):
        return 0.0
    # Midpoint alignment — 1.0 when exactly at exp_mid.
    mid_score = max(0.0, 1.0 - abs(dec_mid - exp_mid) / exp_span)
    # Span similarity — 1.0 when decoded span matches expected span.
    dec_span = dec_hi - dec_lo
    if dec_span <= 0:
        return 0.0
    span_ratio = min(dec_span, exp_span) / max(dec_span, exp_span)
    return 0.6 * mid_score + 0.4 * span_ratio


def _score_unbounded(
    raw_lo: float, raw_hi: float, exp_lo: float, exp_hi: float, scale: float, offset: float
) -> float:
    """Score a one-sided expected range (`More than X` / `Less than X`)."""
    dec_lo = raw_lo * scale + offset
    dec_hi = raw_hi * scale + offset
    if exp_lo == float("-inf"):
        # `Less than exp_hi` — decoded should be entirely below exp_hi
        if dec_hi > exp_hi:
            return 0.0
        # Better fit when decoded sits closer to exp_hi (more of the range used)
        if dec_hi <= 0:
            return 0.3
        proximity = max(0.0, 1.0 - (exp_hi - dec_hi) / max(abs(exp_hi), 1.0))
        return 0.5 + 0.5 * proximity * 0.6  # cap below bounded-match score
    if exp_hi == float("inf"):
        # `More than exp_lo` — decoded should be entirely above exp_lo
        if dec_lo < exp_lo:
            return 0.0
        proximity = max(0.0, 1.0 - (dec_lo - exp_lo) / max(abs(exp_lo), 1.0))
        return 0.5 + 0.5 * proximity * 0.6
    return 0.0


def match_did_range(
    raw_min: int,
    raw_max: int,
    candidates: list[ParamRef],
    *,
    scalings: tuple[float, ...] = _DEFAULT_SCALINGS,
    offsets: tuple[float, ...] = _DEFAULT_OFFSETS,
    min_score: float = 0.5,
    top_k: int = 3,
) -> list[RangeMatch]:
    """Score candidate FSM params against a captured DID's raw value range.

    For each `(param, expected_range, scale, offset)` combination, score
    the fit; keep matches above `min_score`; return the top `top_k` ranked
    descending. Best per (param, expected_range) is kept — we don't double-
    count the same param at two different scalings.
    """
    raw_lo = float(raw_min)
    raw_hi = float(raw_max)
    if raw_hi <= raw_lo:
        # No spread → no signal to fit against.
        return []

    # Per (param, expected_range) keep only the highest-scoring (scale, offset).
    best_per_key: dict[tuple[str, int], RangeMatch] = {}

    for param in candidates:
        # Multi-range params (e.g. MAF: idle 2-6 + revved 7-20) can have a
        # captured range that spans BOTH conditions. A throttle-blip session
        # captures idle→revved as one combined raw range, which won't cleanly
        # fit either sub-range alone but DOES fit their union. Add a synthetic
        # union range to the candidate set so this case scores correctly.
        ranges_to_try: list[tuple[int, ExpectedRange]] = list(
            enumerate(param.expected_ranges)
        )
        bounded = [
            (i, er)
            for i, er in enumerate(param.expected_ranges)
            if isfinite(er.min) and isfinite(er.max)
        ]
        if len(bounded) > 1:
            union = ExpectedRange(
                min=min(er.min for _, er in bounded),
                max=max(er.max for _, er in bounded),
                unit=bounded[0][1].unit,
            )
            ranges_to_try.append((-1, union))

        for er_idx, er in ranges_to_try:
            both_open = not isfinite(er.min) and not isfinite(er.max)
            if both_open:
                continue
            for scale, offset in product(scalings, offsets):
                if isfinite(er.min) and isfinite(er.max):
                    score = _score_bounded(raw_lo, raw_hi, er.min, er.max, scale, offset)
                else:
                    score = _score_unbounded(raw_lo, raw_hi, er.min, er.max, scale, offset)
                if score < min_score:
                    continue
                key = (param.name, er_idx)
                existing = best_per_key.get(key)
                if existing is None or score > existing.score:
                    best_per_key[key] = RangeMatch(
                        param=param,
                        expected=er,
                        scale=scale,
                        offset=offset,
                        decoded_min=raw_lo * scale + offset,
                        decoded_max=raw_hi * scale + offset,
                        score=score,
                    )

    ranked = sorted(best_per_key.values(), key=lambda m: -m.score)
    return ranked[:top_k]

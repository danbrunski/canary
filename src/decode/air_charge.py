"""BFS formation upstream of the X byte, integer for integer (E05.2).

The ECM's base fuel schedule ``0xFFFF103A`` (DID 0x1208, the word the Target
AFR / timing-trim load byte X is divided from) is formed in two tasks of
different dispatch groups (``docs/ecu-firmware-analysis.md``, "BFS formation,
traced (E05.2)"; ``ecu_application_model.yaml`` nodes ``air_charge_tp`` ..
``base_fuel_schedule``):

* group 0x10 (table ``0x02189C``, 67 tasks) runs ``0x03C3A4`` (the Fuel Comp
  load byte ``0xFFFF118A``, :mod:`src.decode.fuel_comp_load`) and then
  ``0x03CC3C``: the fill time constant and coefficient, and the Fuel Comp
  lookup ``0xFFFF225A`` (``0x03D0E6``);
* group 0x20 (table ``0x021874``, 10 tasks) runs ``0x03BB40`` (MAF to the
  airflow long ``0xFFFF2100``) and then ``0x03C62C``: Tp from K, airflow and
  engine speed, the Fuel Comp product, the manifold-filling filter, the
  transient term, the blend and the ceiling; the dispatcher then copies
  ``0xFFFF20A4..20B3`` to ``0xFFFF20B4..20C3`` (``0x020BFC``), which is how the
  filter sees last cycle's value.

Every function here is one of those spans, with the program's shifts,
saturations and wrap-around as code constants (each naming its address).
Calibration values -- K, the Tp multipliers, the tables -- are arguments the
caller reads from the image; nothing here reads a file, a session or a frame.

**Not here, by design.** The airflow long ``0xFFFF2100`` comes out of a
stateful MAF pipeline (``0x03BBF2`` hot-wire correction, ``0x03BB98`` and
``0x03BFC0`` filters, plus the purge term ``0xFFFF273E``) whose state no
capture carries, so it is not computed here; the cranking Tp path (curve ``0xEEBA`` on
coolant) and the MAF-fault BFS table (``0x95E5``) are branches, not modelled.
:func:`steady_state_bfs` and :func:`steady_airflow_interval` are what *is* exact
without them: the filter's unique fixed point, and the airflow word the
PID 0x10 / DID 0x1209 handler (``0x03D2B6``) truncated.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.decode.ecm_lookup import WORD_MAX, lookup_2d

#: Bumped when any function's arithmetic, inputs or refusals change.
AIR_CHARGE_KERNEL_VERSION = 1

U32 = 0xFFFFFFFF
BYTE_MAX = 0xFF
S16_MIN = -0x8000
S16_MAX = 0x7FFF
S32_MIN = -0x80000000
S32_MAX = 0x7FFFFFFF

#: ``0x03C6BA``/``0x03C6CC``: engine speed is floored at 0x40 before the divide.
SPEED_FLOOR = 0x40
#: Literal at ``0x03C888``: the quotient cap, chosen so ``cap x 0x23CB >> 16``
#: is 0xFFFF (Tp fits a word).
TP_QUOTIENT_CAP = 0x726F9
#: ``0x03C748`` (literal ``0x727`` at ``0x03C78A``): a first quotient at or
#: above this takes the cap without the second divide.
TP_FIRST_QUOTIENT_LIMIT = 0x727
#: Literal at ``0x03C78C``: Tp = quotient x 0x23CB >> 16.
TP_SCALE = 0x23CB
TP_SCALE_SHIFT = 16
#: ``0x03C7BC``-``0x03C7C2`` (``shlr2`` x3, ``shlr``): Fuel Comp is 128 = 1.0.
FUEL_COMP_SHIFT = 7
#: ``0x03CF8C`` immediate: the fill coefficient's second gain.
FILL_GAIN_IMMEDIATE = 0x37
#: ``0x03CF96``: the coefficient is capped at 0x7FFF.
FILL_COEFFICIENT_MAX = 0x7FFF
#: ``0x03D05E`` (literal 0x8000 at ``0x03D164``): the filter's unit weight.
FILL_ONE = 0x8000
#: ``0x03C7DC``-``0x03C7E2``: 4 x w105C x a saturates at 0x40000000 x 4.
FILL_PRODUCT_LIMIT = 0x40000000
#: ``0x03C80C``-``0x03C816``: (4ca + pb) >> 15.
FILL_SHIFT = 15
#: ``0x022586``: six arithmetic right shifts.
TRANSIENT_SHIFT = 6
#: ``0x03CA3C`` (``shll8`` of 1): the blend's unit weight.
BLEND_ONE = 0x100
#: ``0x03CE4E`` immediate: the blend weight's floor.
BLEND_WEIGHT_FLOOR = 0x1A
#: ``0x03CE42``-``0x03CE46``: the blend weight is (b1207 x gain) >> 5.
BLEND_WEIGHT_SHIFT = 5
#: ``0x03CAD6`` ``shll8``: the ceiling byte is the high byte of the cap.
CEILING_SHIFT = 8
#: ``0x039048``/``0x03907A``: w1204 = 11BE >> 3, w1210 = 11BE >> 6, so one
#: w1210 count spans eight w1204 counts.
SPEED_WORD_RATIO = 8
#: ``0x03D2C8``: the reported MAF word is (w1056 x scale) >> 8, capped 0xFFFF.
MAF_REPORT_SHIFT = 8


def _whole(name: str, value: int, lo: int, hi: int) -> int:
    if int(value) != value:
        raise ValueError(f"{name} {value!r} is not a whole raw count")
    if not lo <= value <= hi:
        raise ValueError(f"{name} {value!r} is outside {lo}..{hi}")
    return int(value)


def _word(name: str, value: int) -> int:
    return _whole(name, value, 0, WORD_MAX)


def _byte(name: str, value: int) -> int:
    return _whole(name, value, 0, BYTE_MAX)


# -- engine speed for Tp (0x03C646-0x03C6D2) ------------------------------------------


def select_tp_speed(
    *,
    flags_4786: int,
    flags_283b: int,
    speed_1204: int,
    speed_233c: int,
    count_2c91: int,
) -> int:
    """The engine-speed word Tp divides by, before the floor, on the running
    branch (``0x03C686``-``0x03C6D2``): ``count_2c91 << 3`` while
    ``0xFFFF4786`` bit 6 is set, else ``0xFFFF233C`` while ``0xFFFF283B``
    bit 6 is set, else ``0xFFFF1204``.

    ``0xFFFF233C`` is ``w1204`` low-passed by ``0x03907A`` ((old + 255 x new)
    >> 8, plus one when it stalls below), so it equals ``w1204`` once rpm has
    held for a few cycles. Whether the running branch is taken at all
    (``0xFFFF2C91`` non-zero, the start flags) is :func:`running_branch`'s."""
    _byte("flags 0xFFFF4786", flags_4786)
    _byte("flags 0xFFFF283B", flags_283b)
    if flags_4786 & 0x40:
        return min(WORD_MAX, _byte("0xFFFF2C91", count_2c91) << 3)
    if flags_283b & 0x40:
        return _word("w233C", speed_233c)
    return _word("w1204", speed_1204)


def running_branch(*, flags_4786: int, start_hold_4a2d: int, count_2c91: int) -> bool:
    """Whether ``0x03C62C`` computes Tp from airflow (``True``) or from the
    cranking coolant curve ``0xEEBA`` (``False``), ``0x03C646``-``0x03C694``.
    Cranking when ``0xFFFF2C91`` is 0; or ``0xFFFF4786`` bit 7 is set and the
    hold byte ``0xFFFF4A2D`` is non-zero; or ``0xFFFF4786`` bit 6 is set and
    ``0xFFFF2C91`` is below 0x20. (While bit 7 is clear the code first
    reloads ``0xFFFF4A2D`` from cal ``0x8C25``; the caller passes the value
    the test reads.)"""
    flags = _byte("flags 0xFFFF4786", flags_4786)
    count = _byte("0xFFFF2C91", count_2c91)
    hold = _byte("0xFFFF4A2D", start_hold_4a2d)
    if count == 0:
        return False
    if flags & 0x80 and hold != 0:
        return False
    return not (flags & 0x40 and count < 0x20)


# -- Tp (0x03C6D4-0x03C788) -----------------------------------------------------------


@dataclass(frozen=True)
class TpResult:
    #: The capped quotient ``K x Qa x m / 256 / (d x N)`` before the scale.
    quotient: int
    #: ``0xFFFF2110``: quotient x 0x23CB >> 16 (at most 0xFFFF).
    tp: int


def air_charge_tp(
    airflow: int,
    k: int,
    speed: int,
    *,
    flow_multiplier: int,
    speed_multiplier: int,
) -> TpResult:
    """``0xFFFF2110`` on the running branch.

    ``airflow`` is the signed long ``0xFFFF2100``; ``k`` the word at the map
    slot's ``+0x762`` (RAM ``0xFFFFB7A2``); ``speed`` the word
    :func:`select_tp_speed` returns; ``flow_multiplier`` cal ``0x89D2`` (226)
    and ``speed_multiplier`` cal ``0x88AD`` (3).

    The firmware forms ``K x airflow x m / 256`` as a 24.8 number from two
    16 x 16 products, each multiplied by ``m`` in two byte halves, every step
    a 32-bit ``mul.l``/``add`` (wrapping); then divides by ``d x max(0x40,
    speed)`` in two steps -- the integer part (``0x021D6C``), and if that is
    below 0x727, the remainder (``0x021FCC``) with the fraction byte -- and
    caps at 0x726F9. A non-positive airflow gives 0 (``cmp/pl``); a zero
    divisor gives the cap. Below the wrap this is ``min(0x726F9,
    floor(floor(K x airflow x m / 256) / (d x N)))``.
    """
    qa = _whole("airflow 0xFFFF2100", airflow, S32_MIN, S32_MAX)
    kw = _word("K", k)
    m = _byte("flow multiplier (cal 0x89D2)", flow_multiplier)
    d = _byte("speed multiplier (cal 0x88AD)", speed_multiplier)
    n = max(SPEED_FLOOR, _word("speed", speed))
    if qa <= 0:
        return TpResult(quotient=0, tp=0)
    divisor = (d * n) & U32
    if divisor == 0:
        quotient = TP_QUOTIENT_CAP
    else:
        high = (kw * (qa >> 16)) & U32
        low = (kw * (qa & 0xFFFF)) & U32
        r4 = ((((low >> 8) * m) & U32) + ((((low & 0xFF) * m) & U32) >> 8)) & U32
        r1 = (((((high >> 8) * m) & U32) << 8) & U32) + (((high & 0xFF) * m) & U32)
        r1 = (r1 + (r4 >> 8)) & U32
        first = r1 // divisor
        if first >= TP_FIRST_QUOTIENT_LIMIT:
            quotient = TP_QUOTIENT_CAP
        else:
            remainder = r1 % divisor
            second = (((remainder << 8) & U32) + (r4 & 0xFF)) & U32
            quotient = min(TP_QUOTIENT_CAP, ((first << 8) + second // divisor) & U32)
    return TpResult(quotient=quotient, tp=((quotient * TP_SCALE) & U32) >> TP_SCALE_SHIFT)


# -- Fuel Comp (0x03D0E6) and its product (0x03C7AC-0x03C7D2) --------------------------


def rpm_load_lookup(
    cells: Sequence[Sequence[int]],
    rpm_axis: Sequence[int],
    load_axis: Sequence[int],
    rpm_word: int,
    load_byte: int,
) -> int:
    """``0x026A24`` (16 x 16) / ``0x0269AC`` (8 x 8) with raw axis bytes: the
    rpm axis is compared as ``byte << 2`` with the 12.5-rpm word ``0xFFFF1210``
    (``0x027194``), the load axis byte for byte (``0x027114``); bilinear,
    truncating toward the first operand (:func:`src.decode.ecm_lookup.lookup_2d`).
    Used for Fuel Comp (slot ``+0x660`` on ``0xCACD`` x ``0xCB29``, load
    ``0xFFFF118A``) and the fill time constants (``0x9B99``/``0x9BD9`` on
    ``0xC87C`` x ``0xC884``)."""
    rows = [_byte("rpm axis byte", b) << 2 for b in rpm_axis]
    columns = [_byte("load axis byte", b) for b in load_axis]
    return lookup_2d(
        cells, rows, columns, _word("rpm word", rpm_word), _byte("load", load_byte)
    ).value


def fuel_comp_product(tp: int, fuel_comp: int) -> int:
    """``0xFFFF105C = min(0xFFFF, (Tp x FC) >> 7)``, FC the byte ``0xFFFF225A``
    (128 = 1.0). Unconditional while cal ``0x8AE1`` bit 5 selects the table."""
    return min(WORD_MAX, (_word("Tp", tp) * _byte("Fuel Comp", fuel_comp)) >> FUEL_COMP_SHIFT)


# -- the fill coefficient (0x03CF52-0x03D06C) ------------------------------------------


@dataclass(frozen=True)
class FillCoefficient:
    #: ``0xFFFF1164`` (and its copy ``0xFFFF21DA``).
    w1164: int
    #: ``0xFFFF1178``: the new-value weight ``a``.
    a: int
    #: ``0xFFFF2254``: the old-value weight ``b = max(0, 0x8000 - a)``.
    b: int


def fill_coefficient(
    rpm_word: int,
    time_constant: int,
    *,
    gain: int,
    transient_model: bool,
    default: int,
) -> FillCoefficient:
    """The manifold-filling filter's weights, on the branch cal ``0x8D28``
    bit 4 selects (set in every listed image).

    ``w1164 = min(0x7FFF, ((T x gain) x rpm word x 0x37) >> 16)``, each
    product a wrapping 32-bit ``mul.l``; ``T`` is the fill time-constant
    lookup (:func:`rpm_load_lookup` on ``0x9BD9`` while ``0xFFFF2899`` bit 7,
    else ``0x9B99``), ``gain`` cal ``0x8D26`` (58). ``a = w1164`` while
    ``0xFFFF283C`` bit 3 (``transient_model``) is set, else the word at cal
    ``0x9066`` (819)."""
    product = (_byte("time constant", time_constant) * _byte("fill gain (cal 0x8D26)", gain)) & U32
    product = (product * _word("rpm word", rpm_word)) & U32
    product = (product * FILL_GAIN_IMMEDIATE) & U32
    w1164 = min(FILL_COEFFICIENT_MAX, product >> 16)
    a = w1164 if transient_model else _word("fill default (cal 0x9066)", default)
    return FillCoefficient(w1164=w1164, a=a, b=max(0, FILL_ONE - a))


# -- the manifold-filling filter (0x03C7D4-0x03C8B6) -----------------------------------


def fill_filter_step(
    w105c: int,
    a: int,
    b: int,
    previous: int,
    last: int,
    *,
    reverse: bool = False,
) -> int:
    """``0xFFFF20A6`` for one run of ``0x03C62C``.

    ``previous`` is ``0xFFFF20B6`` (the dispatcher's copy of last run's
    ``0xFFFF20A6``) and ``last`` the word ``0xFFFF20A6`` still holds when the
    +1 rule reads it; in ordinary running they are equal.

    Normal (``0xFFFF289C`` bit 5 clear): ``u = 4 x w105C x a`` (all ones from
    ``w105C x a >= 0x40000000``), ``v = previous x b``; ``y = (u + v) >> 15``
    (all ones when the sum would wrap), capped 0xFFFF; if ``y`` is below
    ``min(0xFFFF, 4 x w105C)`` and equals ``last``, ``y + 1``, so the filter
    never stalls short of its target. ``reverse`` (bit 5 set):
    ``y = min(0xFFFF, (v - u) >> 15)``, or 0 when ``u > v``."""
    c = _word("w105C", w105c)
    weight_new = _word("a (0xFFFF1178)", a)
    weight_old = _word("b (0xFFFF2254)", b)
    prior = _word("w20B6", previous)
    stored = _word("w20A6", last)
    product = c * weight_new
    u = U32 if product >= FILL_PRODUCT_LIMIT else product * 4
    v = (prior * weight_old) & U32
    if reverse:
        if u > v:
            return 0
        return min(WORD_MAX, (v - u) >> FILL_SHIFT)
    y = U32 if u >= (~v & U32) else (u + v) >> FILL_SHIFT
    y = min(WORD_MAX, y)
    target = min(WORD_MAX, c << 2)
    if y < target and y == stored:
        y += 1
    return y


def falling_air(w105c: int, filtered: int) -> bool:
    """``0xFFFF2899`` bit 7 (``0x03CB9E``-``0x03CBBE``): ``4 x w105C`` below
    the filter's new value. It selects the decay table ``0x9BD9`` for the
    next fill time-constant lookup."""
    return (_word("w105C", w105c) << 2) < _word("w20A6", filtered)


def steady_state_bfs(w105c: int, ceiling_byte: int) -> int:
    """BFS once airflow, speed, Fuel Comp and the coefficient have held: the
    unique fixed point of :func:`fill_filter_step` (normal branch, ``1 <= a <=
    0x8000``, ``b = 0x8000 - a``) is ``min(0xFFFF, 4 x w105C)``, the
    transient term is then 0 (it is a multiple of this run's change), the
    blend's two operands are equal, so ``0xFFFF103A`` is that, capped at the
    ceiling. (From above the filter falls by ``ceil(a x (y - 4c) / 0x8000)``,
    never past the target; from below it rises, the +1 rule closing any
    stall.) ``0x02D5F4`` seeds the filter at this value at reset."""
    target = min(WORD_MAX, _word("w105C", w105c) << 2)
    return min(target, _byte("ceiling byte", ceiling_byte) << CEILING_SHIFT)


# -- transient, blend and the BFS word (0x03C990-0x03CAE4) ------------------------------


def fill_transient(filtered: int, previous: int, *, gain: int, active: bool) -> int:
    """``0xFFFF105A`` (signed): ``clamp_s16(((w20A6 - w20B6) x gain) >> 6)``
    (arithmetic shift, ``0x022586``), ``gain`` cal ``0x8653`` (160); 0 unless
    ``active`` (:func:`transient_active`)."""
    current = _word("w20A6", filtered)
    prior = _word("w20B6", previous)
    g = _byte("transient gain (cal 0x8653)", gain)
    if not active:
        return 0
    product = (current - prior) * g
    return max(S16_MIN, min(S16_MAX, product >> TRANSIENT_SHIFT))


def transient_active(
    *, flags_283c: int, near_wot: bool, maf_fault_hold: int, throttle_fault_hold: int
) -> bool:
    """``0x03C990``-``0x03C9B8``: the transient term applies only with
    ``0xFFFF283C`` bits 3 and 2 set, the near-WOT flag (``0xFFFF289B`` bit 7)
    clear and both fault hold counters (``0xFFFF20C6``, ``0xFFFF2117``) at 0."""
    flags = _byte("flags 0xFFFF283C", flags_283c)
    return bool(
        flags & 0x08
        and flags & 0x04
        and not near_wot
        and _byte("0xFFFF20C6", maf_fault_hold) == 0
        and _byte("0xFFFF2117", throttle_fault_hold) == 0
    )


def near_wot_next(
    previous: bool | None,
    load_byte: int,
    prior_filtered: int,
    w105c: int,
    *,
    set_at: int,
    clear_below: int,
) -> bool | None:
    """``0xFFFF289B`` bit 7 (``0x03C8B8``-``0x03C8E8``): set when the load byte
    ``0xFFFF118A`` reaches ``set_at`` (cal ``0x8319``, 202) and ``w20B6 >= 4 x
    w105C``; otherwise cleared when the byte is below ``clear_below`` (cal
    ``0x831A``, 200), else kept. ``None`` when it is kept and unknown."""
    byte = _byte("0xFFFF118A", load_byte)
    if byte >= _byte("set threshold", set_at) and _word("w20B6", prior_filtered) >= (
        _word("w105C", w105c) << 2
    ):
        return True
    if byte < _byte("clear threshold", clear_below):
        return False
    return previous


def blend_weight(speed_1207: int, *, gain: int) -> int:
    """``0xFFFF2205 = max(0x1A, min(255, (b1207 x gain) >> 5))``, ``gain`` cal
    ``0x849B`` (255), ``b1207 = min(255, w1204 >> 4)`` (``0x03CE30``)."""
    weighted = (
        _byte("b1207", speed_1207) * _byte("blend gain (cal 0x849B)", gain)
    ) >> BLEND_WEIGHT_SHIFT
    return max(BLEND_WEIGHT_FLOOR, min(BYTE_MAX, weighted))


def fill_blend(filtered: int, previous_blend: int, weight: int, *, active: bool) -> int:
    """``0xFFFF20A4`` (cal ``0x8D28`` bit 4 set): ``(w20A6 x w + w20B4 x
    (256 - w)) >> 8`` while ``0xFFFF283C`` bit 3 is set, bit 2 clear and the
    near-WOT flag set (``active``); else ``w20A6``."""
    current = _word("w20A6", filtered)
    prior = _word("w20B4", previous_blend)
    w = _byte("blend weight 0xFFFF2205", weight)
    if not active:
        return current
    return (current * w + prior * (BLEND_ONE - w)) >> 8


def bfs_word(blend: int, transient: int, ceiling_byte: int) -> int:
    """``0xFFFF103A`` without a MAF fault (``0xFFFF2824`` bit 7 clear):
    ``min(max(0, w20A4 + w105A), ceiling << 8)``, the ceiling byte the curve
    ``0xD8B3`` read by ``0x026BBC`` at ``0xFFFF1207``."""
    total = _word("w20A4", blend) + _whole("w105A", transient, S16_MIN, S16_MAX)
    cap = _byte("ceiling byte", ceiling_byte) << CEILING_SHIFT
    return min(max(0, total), cap)


# -- what was reported (0x03D2B6, 0x03907A) ----------------------------------------------


@dataclass(frozen=True)
class WordInterval:
    lo: int
    hi: int

    @property
    def exact(self) -> bool:
        return self.lo == self.hi


def maf_report_word(w1056: int, *, scale: int) -> int:
    """The word PID 0x10 and DID 0x1209 report: ``min(0xFFFF, (w1056 x
    scale) >> 8)``, ``scale`` cal ``0x83E9`` (149). Both read it (``0x03D2B6``
    writes ``0xFFFF3F70``; the PID handler ``0x0D41B0`` forms the same
    product), so one g/s = 100 reported counts."""
    return min(
        WORD_MAX,
        (_word("w1056", w1056) * _byte("MAF scale (cal 0x83E9)", scale)) >> MAF_REPORT_SHIFT,
    )


def maf_word_interval(reported: int, *, scale: int) -> WordInterval:
    """Every ``w1056`` whose report is ``reported``. A word above ``(0xFFFF x
    scale) >> 8`` is not one the handler can produce (the 0xFFFF cap never
    binds for a scale below 256) and is refused."""
    word = _word("reported MAF word", reported)
    s = _byte("MAF scale (cal 0x83E9)", scale)
    if s == 0:
        raise ValueError("a MAF scale of 0 reports nothing")
    lo = -(-(word << MAF_REPORT_SHIFT) // s)
    hi = -(-((word + 1) << MAF_REPORT_SHIFT) // s) - 1
    if lo > min(hi, WORD_MAX):
        raise ValueError(
            f"no airflow word reports {word} at scale {s}: the handler's largest report "
            f"is {maf_report_word(WORD_MAX, scale=s)}"
        )
    return WordInterval(lo, min(WORD_MAX, hi))


#: ``0x03C18E``-``0x03C1C0``: off the transient fill model, ``0xFFFF20AC =
#: ((L20BC x (256 - g)) >> 4 + (w20A8 x g) >> 4) >> 4`` (``g`` cal ``0x8A68``,
#: 128), which truncates: rising toward ``w20A8`` it stalls one count short,
#: so at a fixed point the airflow long is ``w20A8`` or one below it. The
#: transient branch (lead gain cal ``0x82C2``) and the MAF low-pass
#: ``0x03BB98`` (cal ``0x86D9``, nudged one count when it stalls) settle exactly.
AIRFLOW_FILTER_STALL = 1


def steady_airflow_interval(reported: int, *, scale: int) -> WordInterval:
    """``0xFFFF2100`` at a steady state from the reported MAF word: every
    ``w1056`` that reports it (:func:`maf_word_interval`), widened by the
    :data:`AIRFLOW_FILTER_STALL` the non-transient airflow filter can leave."""
    words = maf_word_interval(reported, scale=scale)
    return WordInterval(max(0, words.lo - AIRFLOW_FILTER_STALL), words.hi)


def speed_1204_interval(rpm_word: int) -> WordInterval:
    """``w1204`` from the 12.5-rpm word ``w1210`` (PID 0x0C / mem
    ``0xFFFF11D8``): both are ``0xFFFF11BE`` shifted (>> 3 and >> 6), so
    ``w1204`` is one of eight counts."""
    word = _word("rpm word", rpm_word)
    lo = word * SPEED_WORD_RATIO
    return WordInterval(min(WORD_MAX, lo), min(WORD_MAX, lo + SPEED_WORD_RATIO - 1))


__all__ = [
    "AIRFLOW_FILTER_STALL",
    "AIR_CHARGE_KERNEL_VERSION",
    "FillCoefficient",
    "TpResult",
    "WordInterval",
    "air_charge_tp",
    "bfs_word",
    "blend_weight",
    "falling_air",
    "fill_blend",
    "fill_coefficient",
    "fill_filter_step",
    "fill_transient",
    "fuel_comp_product",
    "maf_report_word",
    "maf_word_interval",
    "near_wot_next",
    "rpm_load_lookup",
    "running_branch",
    "select_tp_speed",
    "speed_1204_interval",
    "steady_airflow_interval",
    "steady_state_bfs",
    "transient_active",
]

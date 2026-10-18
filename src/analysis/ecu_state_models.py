"""Versioned, explicitly initialised ECM state models (E05.3) and the
timing-trim lookup (E05.4).

Each model reproduces one decoded piece of the ECM program (2008 Xterra,
program ``nissan-1ZS44A-uprev-osiris`` in the profile's
``ecu_application_model.yaml``; the node ids below are that manifest's). The
integer semantics are the firmware's: hysteresis, saturation, update order and
which value of a cycle each comparison reads. Behavioural contracts and
address citations only; the listing stays local.

**No model assumes a history it was not given.** A state the ECM carries
between cycles (a latch, a timer, a committed state, the UpRev selector) is
either *known* — from an observed checkpoint, whose source and acquisition
time travel with it, or because this cycle's inputs determine it whatever it
was — or it is ``None``. A step whose result depends on an unknown state
returns ``None`` with the reason, never a cold-boot or steady-state default:

- a hysteresis latch is determined by its input outside the band
  ``[threshold - hysteresis, threshold)``; inside the band it keeps the
  previous value, so an unknown previous value stays unknown;
- a timer stepped while the tick is unknown, or never observed, is unknown
  until code assigns it outright (several branches zero ``0xFFFF1748``);
- the UpRev selector's debounce byte is last cycle's input, so a map switch
  can be predicted only from an unbroken per-cycle input sequence.

The tick that steps every timer (``0xFFFF84C3`` bit 7) and the fuel task's
cycle period are **not decoded**: transition timing is a measured requirement
(:data:`TIMING_REQUIREMENT`), never fitted to a trace.

Constants are this program's calibration bytes, identical on every image the
manifest lists; the manifest's ``calibration_conditions`` refuse an image
where a byte that makes a branch dead or a value fixed differs
(``check_image`` -> ``unmodelled_logic``), so a constant here cannot silently
describe another calibration.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Generic, Literal, TypeVar

from src.decode.ecm_lookup import (
    FRACTION_BITS,
    LookupResult,
    interpolate_q16,
    lookup_2d,
)

#: Bumped whenever any model's arithmetic, state or refusal changes.
MODEL_VERSION = 1

BYTE_MAX = 0xFF
WORD_MAX = 0xFFFF

#: What transition timing needs and how it will be measured; never fitted.
TIMING_REQUIREMENT = (
    "The rate of the 0xFFFF84C3 bit 7 tick (every enrichment timer step) and the "
    "period of the 0x020EF2 cycle are not decoded. Measure them from same-window "
    "reads of 0xFFFF1742..0xFFFF1749 and 0xFFFF14C4/0xFFFF14C5 while a request is "
    "held: counts per second = 0x80 per tick up, 0x1A per tick down."
)


def _byte(name: str, value: int) -> int:
    if int(value) != value or not 0 <= value <= BYTE_MAX:
        raise ValueError(f"{name} {value!r} is not a byte")
    return int(value)


def _word(name: str, value: int) -> int:
    if int(value) != value or not 0 <= value <= WORD_MAX:
        raise ValueError(f"{name} {value!r} is not a 16-bit word")
    return int(value)


@dataclass(frozen=True)
class Checkpoint:
    """Observed state a model attaches to mid-drive: where it came from and
    when it was acquired. A model never starts from anything else."""

    #: The channel(s) read, e.g. ``mem:ECM_PEDAL_ENRICH_FLAGS``.
    source: str
    #: Acquisition time, epoch seconds.
    acquired_at: float


T = TypeVar("T")


@dataclass(frozen=True)
class Step(Generic[T]):
    """One evaluated cycle: the value, or ``None`` and the reason it is
    unevaluable."""

    value: T | None
    reason: str | None = None

    @property
    def evaluable(self) -> bool:
        return self.value is not None


def latch(previous: bool | None, value: int, threshold: int, hysteresis: int) -> bool | None:
    """A set-high latch with additive hysteresis, as this program writes them:
    set when ``value`` (plus ``hysteresis`` while already set) reaches
    ``threshold``, cleared otherwise. ``None`` only inside the band, where the
    result is the unknown previous value."""
    if value >= threshold:
        return True
    if value + hysteresis < threshold:
        return False
    return previous


# -- 1-D byte curves (0x026BBC) ------------------------------------------------------

#: ``0x026BBC``: a 16-entry byte curve on the implicit axis 0, 16, ..., 240.
CURVE16_ENTRIES = 16
CURVE16_LAST_ENTRY_FROM = 0xF0
CURVE16_INDEX_SHIFT = 4


def byte_curve16(table: Sequence[int], value: int) -> int:
    """``0x026BBC`` (tail ``0x027254``): index ``value >> 4``, fraction the low
    four bits as Q16 (``(value & 15) << 12``), :func:`interpolate_q16` between
    the entry and the next; from 240 up the last entry outright."""
    if len(table) != CURVE16_ENTRIES:
        raise ValueError(f"0x026BBC reads a {CURVE16_ENTRIES}-entry curve, not {len(table)}")
    x = _byte("curve input", value)
    if x >= CURVE16_LAST_ENTRY_FROM:
        return int(table[-1])
    index = x >> CURVE16_INDEX_SHIFT
    fraction = (x & ((1 << CURVE16_INDEX_SHIFT) - 1)) << (FRACTION_BITS - CURVE16_INDEX_SHIFT)
    return interpolate_q16(int(table[index]), int(table[index + 1]), fraction)


# -- E05.3 (1): the pedal enrichment flag, 0xFFFF2841 bit 0 ------------------------------

#: Added to the comparand while the flag is already set (``0x055DEC``).
PEDAL_HYSTERESIS = 8
#: ``0x05535C``: ``0xFFFF3CDC = min(255, (w2D28 x 20571) >> 16)``.
PEDAL_COMPARAND_FACTOR = 20571


def pedal_curve_input(rpm_word: int) -> int:
    """``0xFFFF11CF`` from the observed rpm word ``0xFFFF11D8`` (12.5 rpm per
    count). Both are cycle-start copies from one block (``0xFFFF1207`` and
    ``0xFFFF1210``), written by ``0x03907A`` as ``w1204 >> 4`` (clamped 255)
    and ``w1204 >> 3``: so the byte is ``min(255, rpm_word >> 1)``, 25 rpm per
    count, exactly."""
    return min(BYTE_MAX, _word("rpm word", rpm_word) >> 1)


def pedal_threshold(curve_cedb: Sequence[int], rpm_word: int) -> int:
    """``0xFFFF15F8``: curve ``0xCEDB`` at :func:`pedal_curve_input`."""
    return byte_curve16(curve_cedb, pedal_curve_input(rpm_word))


def pedal_comparand(position_counts: int) -> int:
    """``0xFFFF3CDC`` from ``0xFFFF2D28`` (``0x05535C``, the fuel task's first
    call, so the flag compares this cycle's value). ``0xFFFF2D28`` is
    ``0x07248C``'s throttle position (counts above closed) for the target area
    ``0xFFFF1A64``, through the inverted area curve; so 255 is about 100 % of
    requested opening, not of measured throttle."""
    return min(BYTE_MAX, (_word("position", position_counts) * PEDAL_COMPARAND_FACTOR) >> 16)


def pedal_flag_next(previous: bool | None, comparand: int, threshold: int) -> Step[bool]:
    """``0x055DB2`` at ``0x055DE0``-``0x055DFA``: set when ``0xFFFF3CDC`` (+8
    while set) reaches ``0xFFFF15F8``, unsigned. The previous flag matters only
    in ``[threshold - 8, threshold)``."""
    value = latch(previous, _byte("comparand", comparand), _byte("threshold", threshold), 8)
    if value is None:
        return Step(None, "hysteresis_band_state_unknown")
    return Step(value)


@dataclass
class PedalFlagModel:
    """The flag across cycles. Starts unknown; :meth:`attach` takes an
    observed flag read; :meth:`gap` drops the state when a cycle's comparand
    was not seen (the flag may have moved in between)."""

    curve_cedb: tuple[int, ...]
    flag: bool | None = None
    checkpoint: Checkpoint | None = None

    def attach(self, observed_flag: bool, checkpoint: Checkpoint) -> None:
        self.flag, self.checkpoint = observed_flag, checkpoint

    def gap(self) -> None:
        self.flag = None

    def step(self, comparand: int, rpm_word: int) -> Step[bool]:
        result = pedal_flag_next(self.flag, comparand, pedal_threshold(self.curve_cedb, rpm_word))
        self.flag = result.value
        return result


@dataclass(frozen=True)
class FlagRead:
    """One read of ``0xFFFF2841``, with the same-window comparand
    (``0xFFFF3CDC``) and rpm word when that read carried them."""

    acquired_at: float
    flag: bool
    comparand: int | None = None
    rpm_word: int | None = None


def account_pedal_flag(reads: Sequence[FlagRead], curve_cedb: Sequence[int]) -> dict[str, object]:
    """Score every flag read and count every observed transition, evaluable
    or not. A read is predicted only from inputs read *with* it; the previous
    read is not the previous cycle (reads are ~1 s apart, cycles are not), so
    inside the hysteresis band a read is unevaluable rather than scored
    against an assumed history. Nothing is dropped: ``reads`` and
    ``transitions`` each sum to their totals."""
    flat = len(set(curve_cedb)) == 1
    states: dict[str, int] = {"pass": 0, "miss": 0}
    reasons: dict[str, int] = {}
    transitions = {"observed": 0, "pass": 0, "miss": 0, "unevaluable": 0}
    previous: bool | None = None
    for read in sorted(reads, key=lambda r: r.acquired_at):
        outcome: str
        if read.comparand is None:
            outcome = "input_not_captured:0xFFFF3CDC"
        elif read.rpm_word is None and not flat:
            outcome = "input_not_captured:0xFFFF11D8"
        else:
            threshold = (
                int(curve_cedb[0]) if flat else pedal_threshold(curve_cedb, int(read.rpm_word or 0))
            )
            predicted = pedal_flag_next(None, read.comparand, threshold)
            if predicted.value is None:
                outcome = str(predicted.reason)
            else:
                outcome = "pass" if predicted.value == read.flag else "miss"
        if outcome in states:
            states[outcome] += 1
        else:
            reasons[outcome] = reasons.get(outcome, 0) + 1
        if previous is not None and read.flag != previous:
            transitions["observed"] += 1
            transitions[outcome if outcome in states else "unevaluable"] += 1
        previous = read.flag
    return {
        "model_version": MODEL_VERSION,
        "reads": len(reads),
        "scored": states,
        "unevaluable": reasons,
        "transitions": transitions,
    }


# -- E05.3 (2): UpRev map selection, init and copy ---------------------------------------

UPREV_MAGIC = 0xAA5555AA
#: Bits of ``0xFFFF2EE0`` the selector watches (``0x0DE37E``).
UPREV_INPUT_MASK = 0x0A
UPREV_HOLD = 0x08
UPREV_STEP = 0x02
UPREV_SLOTS = 5
#: The commit sets the countdown to ``selector << 6`` (``shll8``, ``shlr2``).
UPREV_COUNTDOWN_SHIFT = 6


@dataclass(frozen=True)
class UpRevState:
    """UpRev's RAM block at ``0xFFFFB000``: countdown word ``B000``, debounce
    ``B002``, selector ``B003``, magic ``B004``, committed map ``B008``,
    selection history ``B00C``. ``None`` is unobserved."""

    countdown: int | None
    debounce: int | None
    selector: int | None
    magic: int | None
    active_map: int | None
    history: int | None = None


def uprev_power_on(retained: UpRevState) -> tuple[UpRevState, int | None]:
    """``0x0DE1A4`` (from the application reset vector): with the magic absent
    it writes it, zeroes ``B000``-``B00F`` (``B008`` included) and copies slot
    1; with it present it does nothing, so a retained block and its RAM copy
    survive. Returns the state and the slot copied (``None``: no copy).
    Whether RAM was retained across a power-down is not knowable from the
    block (the magic reads the same both ways), so the caller passes what it
    observed before, or an unknown magic, which refuses."""
    if retained.magic is None:
        raise ValueError("power-on needs the retained magic: unknown RAM is not a cold boot")
    if retained.magic == UPREV_MAGIC:
        return retained, None
    return UpRevState(0, 0, 0, UPREV_MAGIC, 0, 0), 1


@dataclass(frozen=True)
class UpRevCycle:
    state: UpRevState
    #: Slot copied to ``0xFFFFB040`` this cycle (a valid commit), or ``None``.
    copied: int | None
    #: ``0xFFFF4858`` bit 0 as written this cycle (``None``: not written).
    indicator: bool | None


def uprev_step(state: UpRevState, inputs_2ee0: int) -> Step[UpRevCycle]:
    """``0x0DE358`` (task 111 of the ``0x020EF2`` loop, after the fuel task):

    - countdown non-zero: decrement it, then ``0xFFFF4858`` bit 0 = bit 5 of
      the new value (the map number blinked, inferred);
    - previous masked input 0: selector = 0; previous 8 (bit 3 alone): bit 1
      now steps the selector, nothing now commits it (countdown = selector x
      64, then ``0x0DE3E4``); any other previous value: nothing;
    - the debounce byte takes this cycle's masked input.

    ``0x0DE3E4``: history = history << 4 | (selector & 15); only 1..5 writes
    ``0xFFFFB008`` and copies slot n from ``0x0E0000 + 0x800 (n - 1)``."""
    inputs = _byte("0xFFFF2EE0", inputs_2ee0) & UPREV_INPUT_MASK
    if state.countdown is None:
        return Step(None, "uprev_countdown_unknown")
    if state.debounce is None:
        return Step(None, "uprev_debounce_unknown")
    countdown, indicator = state.countdown, None
    if countdown:
        countdown -= 1
        indicator = bool(countdown & 0x20)
    selector, active, history, copied = state.selector, state.active_map, state.history, None
    if state.debounce == 0:
        selector = 0
    elif state.debounce == UPREV_HOLD:
        if inputs & UPREV_STEP:
            if selector is None:
                return Step(None, "uprev_selector_unknown")
            selector = (selector + 1) & BYTE_MAX
        elif inputs == 0:
            if selector is None:
                return Step(None, "uprev_selector_unknown")
            countdown = (selector << UPREV_COUNTDOWN_SHIFT) & WORD_MAX
            slot = selector & 0x0F
            history = None if history is None else ((history << 4) | slot) & 0xFFFFFFFF
            if 1 <= slot <= UPREV_SLOTS:
                active, copied = slot, slot
    after = replace(
        state,
        countdown=countdown,
        debounce=inputs,
        selector=selector,
        active_map=active,
        history=history,
    )
    return Step(UpRevCycle(after, copied, indicator))


SlotSource = Literal["committed", "init_default"]


def loaded_slot(magic: int | None, active_map: int | None) -> Step[tuple[int, SlotSource]]:
    """The slot whose bytes the last copy put at ``0xFFFFB040``, from the
    observed magic and ``0xFFFFB008``: a committed 1..5 is that slot; 0 with
    the magic is slot 1 copied by the last init (of the image flashed when it
    ran, which this does not establish: ``ram_copy_unverified``)."""
    if magic is None or active_map is None:
        return Step(None, "uprev_state_unobserved")
    if magic != UPREV_MAGIC:
        return Step(None, "uprev_not_initialised")
    if 1 <= active_map <= UPREV_SLOTS:
        return Step((active_map, "committed"))
    if active_map == 0:
        return Step((1, "init_default"))
    return Step(None, "uprev_active_map_invalid")


def account_uprev_reads(
    reads: Sequence[tuple[float, int | None, int | None, int | None]],
) -> dict[str, object]:
    """Each ``(acquired_at, magic, active_map, selector)`` read of the
    ``0xFFFFB003`` window, classified by :func:`loaded_slot`, and every change
    of ``0xFFFFB008`` between reads counted. A change is never predicted from
    reads alone (the selector debounce needs every cycle's input), so each one
    is counted as unevaluable rather than explained."""
    slots: dict[str, int] = {}
    reasons: dict[str, int] = {}
    changes = {"observed": 0, "unevaluable": 0}
    previous: int | None = None
    for _, magic, active, _selector in sorted(reads, key=lambda r: r[0]):
        result = loaded_slot(magic, active)
        if result.value is None:
            key = str(result.reason)
            reasons[key] = reasons.get(key, 0) + 1
        else:
            slot, source = result.value
            key = f"{slot}:{source}"
            slots[key] = slots.get(key, 0) + 1
        if previous is not None and active is not None and active != previous:
            changes["observed"] += 1
            changes["unevaluable"] += 1
        previous = active if active is not None else previous
    return {
        "model_version": MODEL_VERSION,
        "reads": len(reads),
        "slots": slots,
        "unevaluable": reasons,
        "map_changes": changes,
    }


# -- E05.3 (3): enrichment request and committed state ------------------------------------

#: ``0x055EF0``-``0x056050``: the latch thresholds are calibration bytes
#: (pinned by the manifest's ``calibration_conditions``); the hysteresis
#: amounts are code immediates.
WARM_COOLANT = 0x73  # cal 0x8C98
WARM_HYSTERESIS = 3
RPM_BAND_HYSTERESIS = 4
BAND_LOW = 0x3E  # cal 0x879C; 0xFFFF2875 bit 2: 3,100 rpm at 50 rpm per count
BAND_HIGH = 0x44  # cal 0x879B (bit 1) and 0x87A0 (bit 0): 3,400 rpm
ABOVE_2000 = 0x28  # cal 0x879F; 0xFFFF2876 bit 7: 2,000 rpm
GATE_CLOSED = 0x80
COMMIT_PRESSURE = 0xFD  # 0xFFFF10C5 >= cal 0x879D (0xFE) sets 0xFFFF2866 bit 0
#: Timer steps (code immediates) and the calibration bytes they are compared to.
TIMER_DOWN = 0x1A
TIMER_UP = 0x80
TIMER_1742_HOLD = 25  # cal 0x8794
TIMER_1748_HOLD = 15  # cal 0x8781
TIMER_1742_LEAN = 9  # cal 0x8796


@dataclass(frozen=True)
class EnrichmentLatches:
    warm: bool | None = None  # 0xFFFF288A bit 6
    band_low: bool | None = None  # 0xFFFF2875 bit 2
    band_high: bool | None = None  # 0xFFFF2875 bit 1
    band_high_multiplier: bool | None = None  # 0xFFFF2875 bit 0
    above_2000: bool | None = None  # 0xFFFF2876 bit 7


@dataclass(frozen=True)
class EnrichmentState:
    """What ``0x055DB2`` carries between cycles for the request and commit.
    ``None`` is unknown; nothing defaults."""

    latches: EnrichmentLatches = EnrichmentLatches()
    commit_flag: bool | None = None  # 0xFFFF2866 bit 0
    timer_1742: int | None = None
    timer_1744: int | None = None
    timer_1746: int | None = None
    timer_1748: int | None = None
    committed: int | None = None  # 0xFFFF14C4
    checkpoint: Checkpoint | None = None


@dataclass(frozen=True)
class EnrichmentInputs:
    coolant: int  # 0xFFFF1079, the cycle-start copy of 0xFFFF1083 (raw = degC + 50)
    rpm_coarse: int  # 0xFFFF11CA, 50 rpm per count
    gated_lookup: int  # 0xFFFF174B this cycle
    pedal_flag: bool | None  # 0xFFFF2841 bit 0 this cycle
    pressure_10c5: int | None  # 0xFFFF10C5 (unidentified)
    tick: bool | None  # 0xFFFF84C3 bit 7 (rate not decoded)


@dataclass(frozen=True)
class EnrichmentCycle:
    state: EnrichmentState
    request: int | None  # 0xFFFF14C5
    committed: int | None  # 0xFFFF14C4
    reason: str | None


def _down(value: int | None, tick: bool | None) -> int | None:
    if tick is False:
        return value
    if tick is None or value is None:
        return None
    return max(0, value - TIMER_DOWN)


def _up(value: int | None, tick: bool | None) -> int | None:
    if tick is False:
        return value
    if tick is None or value is None:
        return None
    return min(WORD_MAX, value + TIMER_UP)


def _zero(value: int | None, tick: bool | None) -> int | None:
    if tick is True or value == 0:
        return 0
    return value if tick is False else None


def _hi(value: int | None) -> int | None:
    return None if value is None else value >> 8


def enrichment_request(
    latches: EnrichmentLatches, commit_flag: bool | None, inputs: EnrichmentInputs
) -> tuple[EnrichmentLatches, bool | None, int | None, str | None]:
    """``0x055EF0``-``0x056050``: the latches, ``0xFFFF2866`` bit 0 and the
    request ``0xFFFF14C5``. Returns ``(latches, commit_flag, request, reason)``."""
    coolant = _byte("coolant", inputs.coolant)
    rpm = _byte("rpm byte", inputs.rpm_coarse)
    gated = _byte("gated lookup", inputs.gated_lookup)
    new = EnrichmentLatches(
        warm=latch(latches.warm, coolant, WARM_COOLANT, WARM_HYSTERESIS),
        band_low=latch(latches.band_low, rpm, BAND_LOW, RPM_BAND_HYSTERESIS),
        band_high=latch(latches.band_high, rpm, BAND_HIGH, RPM_BAND_HYSTERESIS),
        band_high_multiplier=latch(
            latches.band_high_multiplier, rpm, BAND_HIGH, RPM_BAND_HYSTERESIS
        ),
        above_2000=latch(latches.above_2000, rpm, ABOVE_2000, RPM_BAND_HYSTERESIS),
    )
    if new.warm is None:
        return new, None, None, "warm_latch_unknown"
    if not new.warm:
        return new, False, 8, None
    if gated == GATE_CLOSED:
        return new, False, 1, None
    if gated < GATE_CLOSED:
        # Unreachable: the gate writes 0x80 or a value of at least 0x81. The
        # firmware leaves the request unchanged; so do we, as unknown.
        return new, commit_flag, None, "gated_lookup_below_0x80"
    if new.band_low is None or (new.band_low and new.band_high is None):
        return new, None, None, "rpm_band_latch_unknown"
    band = 0 if not new.band_low else (1 if not new.band_high else 2)
    if inputs.pedal_flag is None:
        return new, None, None, "pedal_flag_unknown"
    if not inputs.pedal_flag:
        return new, False, 2 + 2 * band, None
    flag = commit_flag
    if inputs.pressure_10c5 is None:
        flag = True if commit_flag is True else None
    elif inputs.pressure_10c5 > COMMIT_PRESSURE:
        flag = True
    return new, flag, 3 + 2 * band, None


def enrichment_commit(state: EnrichmentState, request: int, tick: bool | None) -> EnrichmentCycle:
    """``0x056050``-``0x0566BC``, folded by the calibration conditions the
    manifest pins (cal ``0x877F``, ``0x8780``, ``0x8782``, ``0x8795`` = 0 make
    the timer comparisons against them always pass)."""
    t42, t44, t46, t48 = state.timer_1742, state.timer_1744, state.timer_1746, state.timer_1748
    flag = state.commit_flag
    committed: int | None
    reason: str | None = None
    if request == 1:
        t42, t44, t46, t48 = _down(t42, tick), _down(t44, tick), _down(t46, tick), _zero(t48, tick)
        committed = 1
    elif request == 2:
        h42, h48 = _hi(t42), _hi(t48)
        if h42 is None or h48 is None:
            committed, reason = None, "timer_unknown"
            # Both branches step 0xFFFF1746; only one steps 0xFFFF1748.
            t46, t48 = _down(t46, tick), (t48 if tick is False else None)
        elif h42 < TIMER_1742_HOLD and h48 < TIMER_1748_HOLD:
            t46, t48 = _down(t46, tick), _up(t48, tick)
            committed = 2
        else:
            t46 = _down(t46, tick)
            committed = 3
    elif request in (3, 5, 7):
        if request == 7:
            t44 = _down(t44, tick)
        else:
            t46 = _down(t46, tick)
        if request != 3:
            t48 = _zero(t48, tick)
        committed = None if flag is None else (4 if flag else 3)
        reason = "commit_flag_unknown" if flag is None else None
    elif request == 4:
        h42 = _hi(t42)
        if h42 is None:
            committed, reason = None, "timer_unknown"
            t42, t44, t46, t48 = None, _down(t44, tick), _down(t46, tick), _zero(t48, tick)
        elif h42 < TIMER_1742_HOLD:
            t42, t44, t46, t48 = (
                _up(t42, tick),
                _down(t44, tick),
                _down(t46, tick),
                _zero(t48, tick),
            )
            after = _hi(t42)
            if after is None:
                committed, reason = None, "timer_unknown"
            else:
                committed = 2 if after < TIMER_1742_LEAN else 6
        else:
            t44, t46, t48 = _down(t44, tick), _down(t46, tick), _zero(t48, tick)
            committed = 3
    elif request == 6:
        t44, t48 = _down(t44, tick), _zero(t48, tick)
        committed = 3
    elif request == 8:
        committed = 5
    else:
        raise ValueError(f"request {request} is not one 0x055DB2 writes (1..8)")
    after_state = replace(
        state,
        timer_1742=t42,
        timer_1744=t44,
        timer_1746=t46,
        timer_1748=t48,
        committed=committed,
    )
    return EnrichmentCycle(after_state, request, committed, reason)


def enrichment_step(state: EnrichmentState, inputs: EnrichmentInputs) -> EnrichmentCycle:
    """One cycle of the request and commit, in the firmware's order: latches,
    request (with ``0xFFFF2866`` bit 0), then the commit through the timers."""
    latches, flag, request, reason = enrichment_request(state.latches, state.commit_flag, inputs)
    state = replace(state, latches=latches, commit_flag=flag)
    if request is None:
        # The request byte keeps a value we do not know; the commit reads it.
        unknown = replace(state, timer_1742=None, timer_1744=None, timer_1746=None, timer_1748=None)
        return EnrichmentCycle(replace(unknown, committed=None), None, None, reason)
    return enrichment_commit(state, request, inputs.tick)


# -- E05.4: the timing-trim lookup (an intermediate, never final timing) -----------------

TRIM_VALUE_MASK = 0x7F
TRIM_FLAG = 0x80
#: Cal ``0x8CCD`` / ``0x8CCC``: at or below the first only trim A is read; above
#: the second the selected trim is read; between them the two are blended.
TRIM_COLD_AT_OR_BELOW = 0x6A
TRIM_WARM_ABOVE = 0x6E
#: Rpm axis bytes are compared as ``byte << 2`` against the 12.5 rpm/count word.
TRIM_RPM_AXIS_SCALE = 4


@dataclass(frozen=True)
class TrimLookup:
    #: The byte the wrapper returns: interpolated 7-bit trim | bit 7 of the
    #: lower-left cell.
    value: int
    lookup: LookupResult

    @property
    def trim(self) -> int:
        return self.value & TRIM_VALUE_MASK

    @property
    def flag(self) -> bool:
        return bool(self.value & TRIM_FLAG)


def trim_lookup(
    table: Sequence[Sequence[int]],
    rpm_axis: Sequence[int],
    load_axis: Sequence[int],
    rpm_word: int,
    load_byte: int,
) -> TrimLookup:
    """``0x026A64`` with ``0x026F9C``: the Target AFR searches (``0x027194``
    rpm, axis byte x 4; ``0x027114`` load byte, direct) over 16 breakpoints,
    then the bilinear of ``0x026F40`` on cells masked ``& 0x7F``, with bit 7 of
    the lower-left cell ORed back into the result."""
    masked = [[_byte("trim cell", c) & TRIM_VALUE_MASK for c in row] for row in table]
    result = lookup_2d(
        masked,
        [b * TRIM_RPM_AXIS_SCALE for b in rpm_axis],
        list(load_axis),
        _word("rpm word", rpm_word),
        _byte("load byte", load_byte),
    )
    flag = table[result.row.index][result.column.index] & TRIM_FLAG
    return TrimLookup(result.value | flag, result)


@dataclass(frozen=True)
class SelectedTrim:
    #: ``0xFFFF1777``: the trim the timing sum consumes (``0x04BD24``).
    selected: Step[TrimLookup]
    #: ``0xFFFF177A``: trim A, always.
    main: Step[TrimLookup]


def selected_timing_trim(
    *,
    table_a: Sequence[Sequence[int]],
    table_b: Sequence[Sequence[int]],
    rpm_axis: Sequence[int],
    load_axis: Sequence[int],
    rpm_word: int,
    load_byte: int,
    coolant: int,
    pedal_flag: bool | None,
    high_det: bool | None,
    pedal_column_enabled: bool = True,
) -> SelectedTrim:
    """``0x04E6D8`` (task 66 of the ``0x020EF2`` loop, after the fuel task):

    - load input ``0xFF`` while the pedal flag is set and cal ``0x8ADC`` bit 1
      is set (it is, on every listed image), else the X byte;
    - coolant ``0xFFFF1079`` at or below cal ``0x8CCD`` (106, 56 degC): both
      bytes read trim A;
    - above cal ``0x8CCC`` (110, 60 degC): ``0xFFFF1777`` reads trim B
      (``+0x120``) while ``0xFFFF283F`` bit 6 is set, else A; ``0xFFFF177A``
      reads A;
    - between: a coolant-weighted blend through the signed divide
      ``0x021C48``, whose rounding is not established: unevaluable.

    Which table is UpRev's Main and which High Det stays inferred."""
    coolant = _byte("coolant", coolant)
    if pedal_flag is None and pedal_column_enabled:
        reason = "pedal_flag_unknown"
        return SelectedTrim(Step(None, reason), Step(None, reason))
    load = BYTE_MAX if (pedal_flag and pedal_column_enabled) else load_byte
    a = trim_lookup(table_a, rpm_axis, load_axis, rpm_word, load)
    if coolant <= TRIM_COLD_AT_OR_BELOW:
        return SelectedTrim(Step(a), Step(a))
    if coolant <= TRIM_WARM_ABOVE:
        return SelectedTrim(Step(None, "coolant_blend_band"), Step(None, "coolant_blend_band"))
    if high_det is None:
        return SelectedTrim(Step(None, "high_det_latch_unknown"), Step(a))
    chosen = trim_lookup(table_b, rpm_axis, load_axis, rpm_word, load) if high_det else a
    return SelectedTrim(Step(chosen), Step(a))


# -- E05.4 / E09.7: the ignition chain from the trim to the timing byte ------------------
#
# Seven manifest nodes, each one firmware routine's arithmetic on the words it
# reads. Every word no node identifies is a required argument: nothing here
# holds a typical value for an unidentified term. The chain is additive in
# whole degrees from the trim term to the rate-limited word 0xFFFF232E, which
# is the timing in degrees BTDC (0xFFFF11FF = 110 - 0xFFFF232E, read by PID
# 0x0E and DID 0x110A as 110 - raw), so one trim count moves timing one degree
# wherever no clamp binds: :func:`timing_delta` predicts that, it does not
# measure it.

#: Cal ``0x8B05``: ``0xFFFF27C4`` = min(it, ...) (``0x04D9E4``), and
#: ``0xFFFF11CB`` is ``0xFFFF27C4`` slewed +/-5 per segment (``0x04E5B0``)
#: through the cycle copy, so the trim term's subtrahend is at most this.
#: Pinned by the manifest's ``calibration_conditions`` on ``timing_trim_terms``
#: (an image with another value is refused ``unmodelled_logic``).
TRIM_TERM_SUBTRAHEND_MAX = 7
S8_MIN = -0x80
S8_MAX = 0x7F
#: ``base_timing_sum`` constants (``0x04CB9E``): the trim path subtracts 0x80,
#: the 0xFFFF2833 bit 4 path (0xFFFF27C3, no trim term) 0x40.
BASE_SUM_BIAS = 0x80
ALTERNATE_SUM_BIAS = 0x40
#: ``timing_rate_limit`` (``0x04E0E0``): per-segment rise +4 (+15 with
#: 0xFFFF4785 bit 6, 0 under 0xFFFF28B2 bit 6 or 0xFFFF29E6), fall -15, then
#: cap 50, floor -7, cap 70 (the last never binds after the first).
RATE_STEP_UP = 4
RATE_STEP_UP_FAST = 15
RATE_STEP_DOWN = 15
TIMING_CAP = 50
TIMING_FLOOR = -7
TIMING_LATE_CAP = 70
#: ``0xFFFF99D0`` bit 2's retard, clamped to -10..0 before it is added.
RATE_EXTRA_RETARD_MIN = -10
#: ``timing_final_byte`` (``0x04E4AC``).
FINAL_BYTE_ZERO = 110
FINAL_FLAG_OFFSET = 30
FINAL_FLAG_CAP = 80
FAULT_BYTE_ZERO = 120
FORCED_TIMING_BYTE = 0x5A


def _s8(value: int) -> int:
    return max(S8_MIN, min(S8_MAX, value))


def _signed(name: str, value: int, lo: int, hi: int) -> int:
    if int(value) != value or not lo <= value <= hi:
        raise ValueError(f"{name} {value!r} is outside {lo}..{hi}")
    return int(value)


@dataclass(frozen=True)
class TrimTerms:
    """``timing_trim_terms`` (``0x04BD24``)."""

    #: ``0xFFFF1350``: the selected trim, masked.
    selected: int
    #: ``0xFFFF1351``: max(0, main - selected) with cal ``0x8ADE`` bit 1, else 0.
    pair: int
    #: ``0xFFFF1352``: the main (always trim A) byte, masked.
    main: int
    #: ``0xFFFF2793``: the trim's term in the base timing sum.
    term: int


def timing_trim_terms(
    *, selected: int, main: int, subtrahend_11cb: int, pair_term_enabled: bool
) -> TrimTerms:
    """``0x04BD24`` (called by the base-timing task 74): ``0xFFFF2793 =
    max(0, (0xFFFF1777 & 0x7F) - 0xFFFF11CB)``. ``pair_term_enabled`` is cal
    ``0x8ADE`` bit 1 (clear on every listed image)."""
    sel = _byte("selected trim", selected) & TRIM_VALUE_MASK
    m = _byte("main trim", main) & TRIM_VALUE_MASK
    sub = _byte("0xFFFF11CB", subtrahend_11cb)
    pair = max(0, m - sel) if pair_term_enabled else 0
    return TrimTerms(sel, pair, m, max(0, sel - sub))


def base_timing_sum(
    *,
    alternate_path: bool,
    w1d3e: int,
    w27c6: int,
    w2793: int,
    w279a: int,
    w134a: int,
    w174a: int,
    w1344: int,
    w27c3: int,
) -> int:
    """``0x04CB9E`` into ``0xFFFF1A58`` (signed byte). With 0xFFFF2833 bit 4
    clear: ``1D3E + 27C6 + 2793 - 279A - 134A - 174A - 0x80 + 1344``; with it
    set (``alternate_path``): ``1D3E + 27C3 - 279A - 0x40``, no trim term.
    ``27C6``/``27C3`` are signed bytes, the rest unsigned; clamped -128..127."""
    d3e = _byte("0xFFFF1D3E", w1d3e)
    r79a = _byte("0xFFFF279A", w279a)
    if alternate_path:
        return _s8(d3e + _signed("0xFFFF27C3", w27c3, S8_MIN, S8_MAX) - r79a - ALTERNATE_SUM_BIAS)
    total = (
        d3e
        + _signed("0xFFFF27C6", w27c6, S8_MIN, S8_MAX)
        + _byte("0xFFFF2793", w2793)
        - r79a
        - _byte("0xFFFF134A", w134a)
        - _byte("0xFFFF174A", w174a)
        - BASE_SUM_BIAS
        + _byte("0xFFFF1344", w1344)
    )
    return _s8(total)


def base_timing_select(
    *, w1a58: int, w1a59: int, flag_4785_bit6: bool, flag_2833_bit4: bool, mode_127d: int
) -> int:
    """``0x04CF0C`` into ``0xFFFF27DA``: the trim sum ``0xFFFF1A58``, or the
    coolant-curve value ``0xFFFF1A59`` while 0xFFFF4785 bit 6 or 0xFFFF2833 bit
    4 is set and the mode byte 0xFFFF127D is neither 1 nor 2."""
    mode = _byte("0xFFFF127D", mode_127d)
    if mode not in (1, 2) and (flag_4785_bit6 or flag_2833_bit4):
        return _signed("0xFFFF1A59", w1a59, 0, S8_MAX)
    return _signed("0xFFFF1A58", w1a58, S8_MIN, S8_MAX)


def timing_cycle_copy(w27da: int) -> int:
    """``0x0212C4``: the end-of-cycle block copy ``0xFFFF2792..`` ->
    ``0xFFFF2706..`` puts ``0xFFFF27DA`` in ``0xFFFF274E`` (one cycle later)."""
    return _signed("0xFFFF27DA", w27da, S8_MIN, S8_MAX)


@dataclass(frozen=True)
class RetardCombine:
    """``timing_retard_combine`` (``0x04DF38``)."""

    #: ``0xFFFF2332``: the retard applied.
    retard: int
    #: ``0xFFFF2338``: written only on the plain path (else ``None``).
    w2338: int | None
    #: ``0xFFFF1346``: the combined timing, signed byte.
    combined: int


def timing_retard_combine(
    *,
    w274e: int,
    w1215: int,
    w26d2: int,
    knock_active: bool,
    w272a: int,
    w2334: int,
    w46e6: int,
    flag_28ab_bit4: bool,
    bypass_28b4_bit1: bool,
) -> RetardCombine:
    """``0x04DF38`` (per 120 deg crank segment):

    - ``0xFFFF2332 = min(0xFFFF26D2, -0xFFFF1215)`` while knock control is
      active (0xFFFF28AD bit 5 or 0xFFFF28B3 bit 1), else ``-0xFFFF1215``.
      Both are signed bytes: 0x04DF4C reads 1215 with ``mov.b`` (sign
      extending) and negates it with no ``extu.b``;
    - bypass (0xFFFF28B4 bit 1): ``0xFFFF1346 = min(274E, 127)``;
    - ``0xFFFF46E6`` zero or 0xFFFF28AB bit 4 set: ``2338 = 274E + 2332``,
      ``1346 = s8(2338 + 272A - 2334)``;
    - else ``1346 = s8(274E + min(2332 + 272A, -46E6) - 2334)``.

    The held-step branch (0x04DF78 on, through 0xFFFF28A8 bit 6) is skipped
    because cal 0x8294 is 0 (0x04DF72 ``tst r5,r5; bt 0x4dfb6``); the
    manifest's ``calibration_conditions`` pin that byte, so an image where it
    is not 0 is refused rather than computed here. ``0xFFFF46E6`` is a byte
    (0x04E05E ``mov.b`` then ``extu.b``)."""
    e274 = _signed("0xFFFF274E", w274e, S8_MIN, S8_MAX)
    base_retard = -_signed("0xFFFF1215", w1215, S8_MIN, S8_MAX)
    knock = _signed("0xFFFF26D2", w26d2, S8_MIN, S8_MAX)
    retard = min(knock, base_retard) if knock_active else base_retard
    if bypass_28b4_bit1:
        return RetardCombine(retard, None, min(e274, S8_MAX))
    a72a = _signed("0xFFFF272A", w272a, S8_MIN, S8_MAX)
    w334 = _word("0xFFFF2334", w2334)
    e6 = _byte("0xFFFF46E6", w46e6)
    if e6 == 0 or flag_28ab_bit4:
        w2338 = e274 + retard
        return RetardCombine(retard, w2338, _s8(w2338 + a72a - w334))
    return RetardCombine(retard, None, _s8(e274 + min(retard + a72a, -e6) - w334))


def timing_rate_limit(
    *,
    w1346: int,
    previous_232e: int,
    trim_62b4: int,
    retard_9932: int | None,
    step_up: int,
    first_run: bool,
) -> int:
    """``0x04E0E0`` into ``0xFFFF232E``, one segment. ``trim_62b4`` is
    ``0xFFFF62B4`` when the rpm/load/coolant hysteresis flags (0xFFFF695A
    bits 1-5) are clear, else 0; ``retard_9932`` is ``0xFFFF9932`` while
    0xFFFF99D0 bit 2 is set (clamped to -10..0 here), else ``None``.
    ``step_up`` is the rise allowed this segment (4, 15 with 0xFFFF4785 bit
    6, or 0). Before 0xFFFF28A8 bit 7 is set (``first_run``) the word is 0."""
    if first_run:
        return 0
    if step_up not in (0, RATE_STEP_UP, RATE_STEP_UP_FAST):
        raise ValueError(f"step_up {step_up!r} is none of 0, 4, 15")
    v = _signed("0xFFFF1346", w1346, S8_MIN, S8_MAX) + int(trim_62b4)
    if retard_9932 is not None:
        v += max(RATE_EXTRA_RETARD_MIN, min(0, int(retard_9932)))
    prev = _signed("0xFFFF232E", previous_232e, TIMING_FLOOR, TIMING_LATE_CAP)
    v = min(v, prev + step_up)
    v = max(v, prev - RATE_STEP_DOWN)
    v = min(v, TIMING_CAP)
    return min(max(v, TIMING_FLOOR), TIMING_LATE_CAP)


def timing_rate_limit_steady(value: int, *, step_up: int) -> Step[int]:
    """The fixed point of :func:`timing_rate_limit` at a held input ``value``
    (``0xFFFF1346`` plus its two added terms): the per-segment step does not
    bind there, the caps and the floor do. With a zero rise allowance the
    word keeps whatever history left it below ``value``: unknown."""
    if step_up == 0:
        return Step(None, "rate_limit_rise_disabled")
    return Step(max(TIMING_FLOOR, min(TIMING_CAP, int(value))))


FinalBranch = Literal["normal", "flag_28b1_bit5", "fault_plain", "fault_offset"]


def timing_final_byte(*, w232e: int, branch: FinalBranch, forced_4858_bit5: bool) -> int:
    """``0x04E4AC`` into ``0xFFFF11FF``, the byte that schedules the coil:

    - ``normal``: ``110 - 232E``;
    - ``flag_28b1_bit5``: ``clamp(110 - 232E - 30, 0, 80)``;
    - fault mode (``0x08E498`` non-zero) with 0xFFFF2834 bit 4, 0xFFFF282C
      bit 7 and 0xFFFF2888 bit 0 all clear (``fault_plain``): ``110 - 232E``;
      otherwise (``fault_offset``): ``clamp(120 - 232E, 0, 255)``;
    - 0xFFFF4858 bit 5 then forces ``0x5A``."""
    v = _signed("0xFFFF232E", w232e, TIMING_FLOOR, TIMING_LATE_CAP)
    if forced_4858_bit5:
        return FORCED_TIMING_BYTE
    if branch in ("normal", "fault_plain"):
        return FINAL_BYTE_ZERO - v
    if branch == "flag_28b1_bit5":
        return max(0, min(FINAL_FLAG_CAP, FINAL_BYTE_ZERO - v - FINAL_FLAG_OFFSET))
    if branch == "fault_offset":
        return max(0, min(BYTE_MAX, FAULT_BYTE_ZERO - v))
    raise ValueError(f"final branch {branch!r} is not modelled")


def timing_degrees_btdc(w11ff: int) -> int:
    """The timing byte as PID 0x0E and DID 0x110A report it: 110 - raw."""
    return FINAL_BYTE_ZERO - _byte("0xFFFF11FF", w11ff)


#: What :func:`timing_delta` holds equal on both ROMs, and the path it is on.
#: A result carries these, so a delta is never read without them.
TIMING_DELTA_CONDITIONS: tuple[str, ...] = (
    "every chain term but the trim term 0xFFFF2793 is equal on both ROMs "
    "(0xFFFF1D3E, 27C6, 279A, 134A, 174A, 1344, 11CB, 1215, 26D2, 272A, 2334, 46E6, "
    "62B4, 9932): unidentified, never given a value",
    "trim path: 0xFFFF2833 bit 4 and 0xFFFF4785 bit 6 clear (else 0xFFFF27C3 or "
    "0xFFFF1A59 replaces the trim sum), no 0xFFFF28B4 bit 1 bypass",
    "rate limit at its fixed point with a nonzero rise allowance; final byte on "
    "the 110 - 0xFFFF232E branch, 0xFFFF4858 bit 5 clear",
    "intermediate signed-byte clamps (0xFFFF1A58, 0xFFFF1346) not binding",
)

TimingStatus = Literal["computed", "delta_if_no_clamp", "unevaluable"]


@dataclass(frozen=True)
class TimingDelta:
    """Base and candidate ignition timing at one point, degrees BTDC."""

    status: TimingStatus
    #: Candidate minus base, degrees; ``None`` when unevaluable.
    delta_deg: int | None
    #: The trim term's own change (0xFFFF2793), counts; set whenever both
    #: selected trims are known, even where ``delta_deg`` cannot be.
    trim_term_delta: int | None
    base_deg: int | None
    candidate_deg: int | None
    reasons: tuple[str, ...]


def timing_delta(
    base: Step[TrimLookup],
    candidate: Step[TrimLookup],
    *,
    base_timing_deg: int | None = None,
    held_reasons: Sequence[str] = (),
) -> TimingDelta:
    """The ECU-side timing change from the two selected trims (``0xFFFF1777``)
    with every other term held equal (:data:`TIMING_DELTA_CONDITIONS`).

    - The trim term is ``max(0, trim - 0xFFFF11CB)`` with ``0xFFFF11CB`` in
      0..7 (cal ``0x8B05``): its change is the trims' change exactly when both
      trims are at least 7, whatever ``0xFFFF11CB`` is; otherwise the floor
      may bind and nothing is claimed.
    - ``base_timing_deg`` (the base ROM's logged timing at the point, where a
      design has it) gives both absolutes, with the 50 cap and -7 floor
      applied; a base at a clamp leaves the change unknown. Without it, the
      change holds only if no clamp binds, and the result says so
      (``clamp_headroom_unknown``).
    - ``held_reasons`` are what the caller knows moves a held term (an X
      change moves 0xFFFF27C6): any one leaves the change unevaluable, the
      trim term's change still reported."""
    if base.value is None or candidate.value is None:
        reasons = tuple(dict.fromkeys(r for r in (base.reason, candidate.reason) if r))
        return TimingDelta("unevaluable", None, None, None, None, reasons)
    tb, tc = base.value.trim, candidate.value.trim
    if min(tb, tc) < TRIM_TERM_SUBTRAHEND_MAX:
        return TimingDelta("unevaluable", None, None, None, None, ("trim_term_floor_may_bind",))
    d = tc - tb
    if held_reasons:
        return TimingDelta("unevaluable", None, d, None, None, tuple(held_reasons))
    if base_timing_deg is None:
        reasons = ("clamp_headroom_unknown",) if d else ()
        return TimingDelta("delta_if_no_clamp", d, d, None, None, reasons)
    b = int(base_timing_deg)
    if not TIMING_FLOOR < b < TIMING_CAP:
        return TimingDelta("unevaluable", None, d, b, None, ("base_timing_at_clamp",))
    c = max(TIMING_FLOOR, min(TIMING_CAP, b + d))
    reasons = ()
    if c != b + d:
        reasons = ("timing_cap_binds",) if c == TIMING_CAP else ("timing_floor_binds",)
    return TimingDelta("computed", c - b, d, b, c, reasons)


__all__ = [
    "ALTERNATE_SUM_BIAS",
    "BASE_SUM_BIAS",
    "FINAL_BYTE_ZERO",
    "MODEL_VERSION",
    "RATE_STEP_DOWN",
    "RATE_STEP_UP",
    "RATE_STEP_UP_FAST",
    "TIMING_CAP",
    "TIMING_DELTA_CONDITIONS",
    "TIMING_FLOOR",
    "TIMING_REQUIREMENT",
    "TRIM_TERM_SUBTRAHEND_MAX",
    "Checkpoint",
    "EnrichmentCycle",
    "EnrichmentInputs",
    "EnrichmentLatches",
    "EnrichmentState",
    "FinalBranch",
    "FlagRead",
    "PedalFlagModel",
    "RetardCombine",
    "SelectedTrim",
    "Step",
    "TimingDelta",
    "TimingStatus",
    "TrimLookup",
    "TrimTerms",
    "UpRevCycle",
    "UpRevState",
    "account_pedal_flag",
    "account_uprev_reads",
    "base_timing_select",
    "base_timing_sum",
    "byte_curve16",
    "enrichment_commit",
    "enrichment_request",
    "enrichment_step",
    "latch",
    "loaded_slot",
    "pedal_comparand",
    "pedal_curve_input",
    "pedal_flag_next",
    "pedal_threshold",
    "selected_timing_trim",
    "timing_cycle_copy",
    "timing_degrees_btdc",
    "timing_delta",
    "timing_final_byte",
    "timing_rate_limit",
    "timing_rate_limit_steady",
    "timing_retard_combine",
    "timing_trim_terms",
    "trim_lookup",
    "uprev_power_on",
    "uprev_step",
]

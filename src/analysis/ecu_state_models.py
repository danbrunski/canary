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


__all__ = [
    "MODEL_VERSION",
    "TIMING_REQUIREMENT",
    "Checkpoint",
    "EnrichmentCycle",
    "EnrichmentInputs",
    "EnrichmentLatches",
    "EnrichmentState",
    "FlagRead",
    "PedalFlagModel",
    "SelectedTrim",
    "Step",
    "TrimLookup",
    "UpRevCycle",
    "UpRevState",
    "account_pedal_flag",
    "account_uprev_reads",
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
    "trim_lookup",
    "uprev_power_on",
    "uprev_step",
]

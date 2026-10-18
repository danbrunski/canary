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


#: ``0x026BA4``: an 8-entry byte curve on the implicit axis 0, 32, ..., 224.
CURVE8_ENTRIES = 8
CURVE8_LAST_ENTRY_FROM = 0xE0
CURVE8_INDEX_SHIFT = 5


def byte_curve8(table: Sequence[int], value: int) -> int:
    """``0x026BA4`` (tail ``0x027250``, ``0x027254`` with one more shift):
    index ``value >> 5``, fraction the low five bits as Q16
    (``(value & 31) << 11``), :func:`interpolate_q16` between the entry and the
    next; from 224 up the last entry outright."""
    if len(table) != CURVE8_ENTRIES:
        raise ValueError(f"0x026BA4 reads a {CURVE8_ENTRIES}-entry curve, not {len(table)}")
    x = _byte("curve input", value)
    if x >= CURVE8_LAST_ENTRY_FROM:
        return int(table[-1])
    index = x >> CURVE8_INDEX_SHIFT
    fraction = (x & ((1 << CURVE8_INDEX_SHIFT) - 1)) << (FRACTION_BITS - CURVE8_INDEX_SHIFT)
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
      ``0x021C48`` (truncation toward zero, resolved 2026-10-17; the blend
      is not implemented yet): unevaluable.

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


# -- E09.7: the base timing term 0xFFFF27C6 and the inputs of 0xFFFF279A ------------------
#
# The term that carries most of the base advance, resolved from the listing
# (firmware notes 2026-10-17). A burn-time model: the rate map 0x9AF9 (rpm x X)
# times a coolant curve and a mixture curve, plus an rpm term, divides a
# charge term, and the quotient (a time in 1/128 ms) is turned into crank
# angle at the current rpm. Every RAM input is a required argument and every
# calibration value is passed in, read from the image by the caller; only the
# immediates of the code are constants here.
#
# Divides: the signed helper 0x021C48 divides magnitudes and negates when the
# signs differ (truncates toward zero, :func:`_sdiv`); the unsigned helper
# 0x021D6C floors (:func:`_udiv`). Both take the dividend in r1 and the
# divisor in r0.

#: ``0x04BA6C``: the ``0xFFFF1B24`` band that indexes the two mixture curves.
CHARGE_INDEX_FROM = 0x2000
CHARGE_INDEX_FULL_FROM = 0x6000
CHARGE_INDEX_SHIFT = 6
#: ``0x04B9D0``: immediates of the charge term (divisor 29 on 0xFFFF1B24, the
#: ``>> 5`` that makes 0xFFFF15F7, the 0x8000 reciprocal of 0xFFFF10EB, the
#: divisor 25 on the product, the ``>> 7`` of 0xFFFF1512).
CHARGE_Q_DIVISOR = 29
CHARGE_Q_SHIFT = 5
CHARGE_RECIPROCAL = 0x8000
CHARGE_PRODUCT_DIVISOR = 25
CHARGE_1512_SHIFT = 7
#: ``0x04BA6C`` immediates: the ratio's ``<< 20`` / ``>> 9`` / ``+ 0x2000``, the
#: factor ``0x1000 - (cal x (0x4000 - R) >>a 8)``, the divisors 1000 and 625 and
#: the ``<< 6`` of the rpm term.
RATE_RATIO_SHIFT = 20
RATE_RATIO_PRODUCT_SHIFT = 9
RATE_RATIO_BIAS = 0x2000
RATE_FACTOR_ONE = 0x1000
RATE_FACTOR_REFERENCE = 0x4000
RATE_FACTOR_SHIFT = 8
RATE_MAP_DIVISOR = 1000
RATE_RPM_SHIFT = 6
RATE_RPM_DIVISOR = 625
#: Q14 products (``>> 14``) in 0x04B98A and 0x04BA6C.
Q14_SHIFT = 14
#: ``0x04BC5A`` immediates: ``x 120`` (``x 15``, ``shll2``, ``shll``), ``x 5 >> 1``
#: on cal 0x8B0C, ``>> 10``, ``x 3 / 5``, and the ``v - 14`` / cap 127 at 141.
TERM_TIME_FACTOR = 120
TERM_OFFSET_FACTOR = 5
TERM_RPM_SHIFT = 10
TERM_ANGLE_NUMERATOR = 3
TERM_ANGLE_DENOMINATOR = 5
TERM_BIAS = 14
TERM_CAP_ABOVE = 141
TERM_CAP = 127
#: ``0x04C7A0``: 0xFFFF26D0 is the four-byte product ``>> 24``; ``0x04C580``:
#: 0xFFFF26D9 the two-byte product ``>> 8``; ``0x04C952``: 0xFFFF17FA ``>> 4``.
TERM_26D0_SHIFT = 24
TERM_26D9_SHIFT = 8
TERM_17FA_SHIFT = 4
#: ``0x04C7A0``: 0xFFFF1302's curve input is ``min(255, w1038 >> 7)``.
BFS_CURVE_SHIFT = 7
#: ``0x03A1F0``: 0xFFFF1081 (cycle copy 0xFFFF1077) rescales the coolant byte
#: 0xFFFF1083 (degC + 50): 0 below 10, 255 from 170, else (c - 10) x 240 / 150.
COOLANT_RESCALE_FROM = 10
COOLANT_RESCALE_FULL_FROM = 170
COOLANT_RESCALE_NUMERATOR = 240
COOLANT_RESCALE_DENOMINATOR = 150

U32 = 0xFFFFFFFF


def _sdiv(dividend: int, divisor: int) -> int:
    """``0x021C48``: signed divide, truncating toward zero."""
    quotient = abs(dividend) // abs(divisor)
    return -quotient if (dividend < 0) != (divisor < 0) else quotient


def _udiv(dividend: int, divisor: int) -> int:
    """``0x021D6C``: unsigned divide (floor) of two non-negative words."""
    return (dividend & U32) // (divisor & U32)


def _s16(value: int) -> int:
    """The low 16 bits, sign-extended (``exts.w``)."""
    return ((value + 0x8000) & WORD_MAX) - 0x8000


def _s32(value: int) -> int:
    return ((value + 0x80000000) & U32) - 0x80000000


def coolant_rescaled(raw_1083: int) -> int:
    """``0x03A1F0``: the rescaled coolant byte 0xFFFF1081 (cycle copy
    0xFFFF1077, degC = byte x 0.625 - 40) from the degC + 50 byte 0xFFFF1083:
    0 below 10, 255 from 170, else ``(c - 10) x 240 / 150`` through the signed
    divide 0x021C48 (positive, so a floor). Not 0xFFFF1079, the degC + 50 copy
    the trim selection reads."""
    c = _byte("0xFFFF1083", raw_1083)
    if c < COOLANT_RESCALE_FROM:
        return 0
    if c >= COOLANT_RESCALE_FULL_FROM:
        return BYTE_MAX
    return _sdiv(
        (c - COOLANT_RESCALE_FROM) * COOLANT_RESCALE_NUMERATOR, COOLANT_RESCALE_DENOMINATOR
    )


def timing_term_1512(b26f2: int, cal_8538: int) -> int:
    """``0x04B96C``: ``0xFFFF1512 = 0xFFFF26F2 x cal 0x8538`` (``mulu.w``, low
    16 bits). Cal 0x8538 is 0 on every listed image, so the word is 0 there."""
    return (_byte("0xFFFF26F2", b26f2) * _byte("cal 0x8538", cal_8538)) & WORD_MAX


def timing_charge_word_1b24(w26f6: int, w26e8: int, w26c0: int) -> int:
    """``0x04B98A``: ``0xFFFF1B24 = min(0xFFFF, max(0, 0xFFFF26F6 -
    0xFFFF26E8) x 0xFFFF26C0 >> 14)``: the correction sum less 26E8, times the
    enrichment multiplier (Q14, 0x4000 = 1.0)."""
    base = max(0, _word("0xFFFF26F6", w26f6) - _word("0xFFFF26E8", w26e8))
    return min(WORD_MAX, (base * _word("0xFFFF26C0", w26c0)) >> Q14_SHIFT)


def timing_charge_map_10eb(
    table_9b89: Sequence[Sequence[int]],
    rpm_axis_c890: Sequence[int],
    load_axis_c88c: Sequence[int],
    rpm_word: int,
    load_byte: int,
) -> int:
    """``0x03D110`` through ``0x026934``: map 0x9B89 (4 x 4, q7: 0x80 = 1.0)
    at the rpm word 0xFFFF1210 (searched by 0x027194 against the axis 0xC890
    as byte x 4) and the X byte 0xFFFF104E (0x027114 against 0xC88C), into
    0xFFFF1177. 0xFFFF10EB is its cycle copy (0xFFFF1124.. -> 0xFFFF1098..,
    n 0x8C), so in a steady state 0xFFFF10EB is this lookup at the snapshot's
    rpm word and X. Computed inside the Fuel Comp routine; its only readers
    (via 0xFFFF10EB) are 0x04B9D0 and 0x04BA6C, so it is a timing table."""
    result = lookup_2d(
        [[_byte("0x9B89 cell", c) for c in row] for row in table_9b89],
        [b * TRIM_RPM_AXIS_SCALE for b in rpm_axis_c890],
        list(load_axis_c88c),
        _word("rpm word", rpm_word),
        _byte("X byte", load_byte),
    )
    return result.value


def timing_load_curve_19cb(curve_cd9b: Sequence[int], x_103e: int) -> int:
    """``0x04BA52``: ``0xFFFF19CB`` = the 8-entry curve 0xCD9B at the X byte
    0xFFFF103E. The curve is 00 20 .. E0 on every listed image, so it is
    ``min(X, 224)`` there."""
    return byte_curve8(curve_cd9b, x_103e)


@dataclass(frozen=True)
class ChargeTerm:
    """``0x04B9D0``."""

    #: ``0xFFFF17C6``.
    w17c6: int
    #: ``0xFFFF15F7``: ``(0xFFFF1B24 / 29) >> 5`` as a byte.
    b15f7: int


def timing_charge_term_17c6(*, x_103e: int, w1b24: int, b10eb: int, w1512: int) -> ChargeTerm:
    """``0x04B9D0``: ``q = 0xFFFF1B24 / 29`` (signed divide);
    ``0xFFFF15F7 = q >> 5``; ``0xFFFF17C6 = 0xFFFF`` when 0xFFFF10EB is 0,
    else ``min(0xFFFF, X x ((0xFFFF1512 >> 7) + (q >> 5 & 0xFF) + 0x8000 /
    0xFFFF10EB) / 25)``, both divides signed (truncating toward zero)."""
    x = _byte("0xFFFF103E", x_103e)
    q = _sdiv(_word("0xFFFF1B24", w1b24), CHARGE_Q_DIVISOR)
    b15f7 = (q >> CHARGE_Q_SHIFT) & BYTE_MAX
    b10 = _byte("0xFFFF10EB", b10eb)
    w12 = _word("0xFFFF1512", w1512)
    if b10 == 0:
        return ChargeTerm(WORD_MAX, b15f7)
    total = (w12 >> CHARGE_1512_SHIFT) + b15f7 + _sdiv(CHARGE_RECIPROCAL, b10)
    return ChargeTerm(min(WORD_MAX, _sdiv(x * total, CHARGE_PRODUCT_DIVISOR)), b15f7)


@dataclass(frozen=True)
class RateTerm:
    """``0x04BA6C``: 0xFFFF15DE and every intermediate it stores or forms, so
    a read of any of them can be checked."""

    #: ``0xFFFF15DE``.
    w15de: int
    #: ``0xFFFF15DA = min(255, 15D7 x 15DC x 15DB >> 14)``.
    b15da: int
    #: ``0xFFFF15DB``: map 0x9AF9 at (rpm/50, X).
    b15db: int
    #: ``0xFFFF15D7``: curve 0xCEAB at the charge index.
    b15d7: int
    #: ``0xFFFF15DC``: curve 0xCEBB at the rescaled coolant byte.
    b15dc: int
    #: ``0xFFFF1CC9``: curve 0xD9AF at the rescaled coolant byte.
    b1cc9: int
    #: ``0xFFFF19B6``: curve 0xD60F at the charge index.
    b19b6: int
    #: ``r``: 0 below 0x2000, 255 from 0x6000, else ``(0xFFFF1B24 - 0x2000) >> 6``.
    charge_index: int
    #: ``R = (10EB << 20) / (((1512 x 10EB) >> 9) + 0x2000)`` (unsigned), low 16 bits signed.
    ratio: int
    #: ``F = s16(0x1000 - (cal 0x8440 x (0x4000 - R) >>a 8))``.
    factor: int
    #: ``S = clamp(15DA x F / 1000 + (cal 0x880E x rpm word << 6) / 625, 0, 0xFFFF)``.
    rate: int


def timing_rate_word_15de(
    *,
    coolant_1077: int,
    w1b24: int,
    rpm50_11ca: int,
    x_103e: int,
    rpm_word_11d8: int,
    b10eb: int,
    w1512: int,
    rate_map_9af9: Sequence[Sequence[int]],
    rpm_axis_c890: Sequence[int],
    load_axis_c88c: Sequence[int],
    coolant_curve_cebb: Sequence[int],
    charge_curve_ceab: Sequence[int],
    coolant_curve_d9af: Sequence[int],
    charge_curve_d60f: Sequence[int],
    cal_8440: int,
    cal_880e: int,
) -> RateTerm:
    """``0x04BA6C``, with ``c`` the RESCALED coolant byte 0xFFFF1077
    (:func:`coolant_rescaled`; not degC + 50) and ``A`` = 0xFFFF1B24:

    - ``15DC = curve8(0xCEBB, c)``; ``r`` = 0 if ``A < 0x2000``, 255 if
      ``A >= 0x6000``, else ``(A - 0x2000) >> 6``; ``15D7 = curve8(0xCEAB, r)``;
    - ``15DB`` = map 0x9AF9 at (rpm/50 byte 0xFFFF11CA, X 0xFFFF103E) through
      0x0268B8, which searches BOTH axes with 0x027114 (the rpm axis 0xC890
      compared with the rpm/50 byte directly, unlike 0x9B89);
    - ``15DA = min(255, 15D7 x 15DC x 15DB >> 14)``;
    - ``R = udiv(10EB << 20, ((1512 x 10EB) >> 9) + 0x2000)``, sign-extended
      from 16 bits; ``F = s16(0x1000 - (cal 0x8440 x (0x4000 - R) >>a 8))``
      (an arithmetic shift);
    - ``S = clamp(sdiv(15DA x F, 1000) + sdiv((cal 0x880E x 11D8) << 6, 625),
      0, 0xFFFF)``;
    - ``1CC9 = curve8(0xD9AF, c)``; ``19B6 = curve8(0xD60F, r)``;
      ``15DE = min(0xFFFF, S x 19B6 x 1CC9 >> 14)``."""
    c = _byte("0xFFFF1077 (rescaled coolant)", coolant_1077)
    a = _word("0xFFFF1B24", w1b24)
    b10 = _byte("0xFFFF10EB", b10eb)
    w12 = _word("0xFFFF1512", w1512)
    rpm_word = _word("0xFFFF11D8", rpm_word_11d8)
    if a < CHARGE_INDEX_FROM:
        r = 0
    elif a >= CHARGE_INDEX_FULL_FROM:
        r = BYTE_MAX
    else:
        r = (a - CHARGE_INDEX_FROM) >> CHARGE_INDEX_SHIFT
    b15dc = byte_curve8(coolant_curve_cebb, c)
    b15d7 = byte_curve8(charge_curve_ceab, r)
    b15db = lookup_2d(
        [[_byte("0x9AF9 cell", v) for v in row] for row in rate_map_9af9],
        list(rpm_axis_c890),
        list(load_axis_c88c),
        _byte("0xFFFF11CA", rpm50_11ca),
        _byte("0xFFFF103E", x_103e),
    ).value
    b15da = min(BYTE_MAX, (b15d7 * b15dc * b15db) >> Q14_SHIFT)
    ratio = _s16(
        _udiv(
            b10 << RATE_RATIO_SHIFT,
            ((w12 * b10) >> RATE_RATIO_PRODUCT_SHIFT) + RATE_RATIO_BIAS,
        )
    )
    product = _s32(_byte("cal 0x8440", cal_8440) * (RATE_FACTOR_REFERENCE - ratio))
    factor = _s16(RATE_FACTOR_ONE - (product >> RATE_FACTOR_SHIFT))
    rpm_term = _sdiv((_byte("cal 0x880E", cal_880e) * rpm_word) << RATE_RPM_SHIFT, RATE_RPM_DIVISOR)
    rate = max(0, min(WORD_MAX, _sdiv(b15da * factor, RATE_MAP_DIVISOR) + rpm_term))
    b1cc9 = byte_curve8(coolant_curve_d9af, c)
    b19b6 = byte_curve8(charge_curve_d60f, r)
    w15de = min(WORD_MAX, (rate * b19b6 * b1cc9) >> Q14_SHIFT)
    return RateTerm(w15de, b15da, b15db, b15d7, b15dc, b1cc9, b19b6, r, ratio, factor, rate)


def timing_term_27c6(
    *, w17c6: int, w15de: int, b19cb: int, rpm_word: int, cal_880d: int, cal_8b0c: int
) -> int:
    """``0x04BC5A``, into the signed byte 0xFFFF27C6 (degrees):

    - ``t = 0xFFFF`` when ``15DE x 19CB`` is 0, else
      ``udiv(cal 0x880D x 120 x 17C6, 15DE x 19CB)``;
    - ``u = t + (cal 0x8B0C x 5 >> 1)``: a time in 1/128 ms [inferred];
    - below 0x10000, ``v = udiv(((11D8 x u) >> 10) x 3, 5)``; from 0x10000,
      ``v = sdiv(((11D8 x 0xFFFF) >>a 10) x 3, 5)``;
    - ``v - 14`` while ``v <= 141``, else 127 (immediates, not calibration).
      The compare is unsigned (``cmp/hi`` at 0x04BD0A), so a negative ``v``
      (only from the second branch, with an rpm word above 0x8000) reads as
      a large unsigned value and gives 127.

    Degrees = rpm x time: ``v`` is the crank angle swept in ``u x 7.8125 us``
    [inferred]."""
    t17 = _word("0xFFFF17C6", w17c6)
    divisor = _word("0xFFFF15DE", w15de) * _byte("0xFFFF19CB", b19cb)
    rpm = _word("0xFFFF11D8", rpm_word)
    if divisor == 0:
        t = WORD_MAX
    else:
        t = _udiv(_byte("cal 0x880D", cal_880d) * TERM_TIME_FACTOR * t17, divisor)
    u = t + ((_byte("cal 0x8B0C", cal_8b0c) * TERM_OFFSET_FACTOR) >> 1)
    if u <= WORD_MAX:
        v = _udiv(((rpm * u) >> TERM_RPM_SHIFT) * TERM_ANGLE_NUMERATOR, TERM_ANGLE_DENOMINATOR)
    else:
        v = _sdiv(
            (_s32(rpm * WORD_MAX) >> TERM_RPM_SHIFT) * TERM_ANGLE_NUMERATOR,
            TERM_ANGLE_DENOMINATOR,
        )
    return v - TERM_BIAS if (v & U32) <= TERM_CAP_ABOVE else TERM_CAP


@dataclass(frozen=True)
class BaseTimingTables:
    """The ROM tables the 0xFFFF27C6 chain reads (rom_map ids in comments),
    as raw bytes read from the image."""

    #: ``base_timing_rate_map`` (0x9AF9), 4 x 4, rows the rpm axis.
    rate_map_9af9: tuple[tuple[int, ...], ...]
    #: ``base_timing_charge_map`` (0x9B89), 4 x 4, the same axes.
    charge_map_9b89: tuple[tuple[int, ...], ...]
    #: ``base_timing_rate_rpm_axis`` (0xC890): rpm/50 bytes.
    rpm_axis_c890: tuple[int, ...]
    #: ``base_timing_rate_load_axis`` (0xC88C): X bytes.
    load_axis_c88c: tuple[int, ...]
    #: ``base_timing_load_curve`` (0xCD9B), 8 entries, at X.
    load_curve_cd9b: tuple[int, ...]
    #: ``base_timing_coolant_curve`` (0xCEBB), 8 entries, at the rescaled coolant byte.
    coolant_curve_cebb: tuple[int, ...]
    #: ``base_timing_charge_curve`` (0xCEAB), 8 entries, at the charge index.
    charge_curve_ceab: tuple[int, ...]
    #: ``base_timing_rate_coolant_curve`` (0xD9AF), 8 entries.
    coolant_curve_d9af: tuple[int, ...]
    #: ``base_timing_rate_charge_curve`` (0xD60F), 8 entries.
    charge_curve_d60f: tuple[int, ...]


@dataclass(frozen=True)
class BaseTimingCals:
    """The calibration bytes of the 0xFFFF27C6 chain, read from the image."""

    #: 179 on every listed image.
    cal_880d: int
    #: 25.
    cal_880e: int
    #: 32.
    cal_8440: int
    #: 14.
    cal_8b0c: int


@dataclass(frozen=True)
class Term279aTables:
    """The ROM tables of 0xFFFF279A's inputs (rom_map ids in comments), raw
    bytes read from the image."""

    #: ``timing_retard_26d9_map`` (0x9B09), 8 x 8, rows the rpm byte 0xFFFF11CE.
    map_9b09: tuple[tuple[int, ...], ...]
    #: ``timing_retard_26d9_rpm_axis`` (0xC8FC).
    rpm_axis_c8fc: tuple[int, ...]
    #: ``timing_retard_26d9_coolant_axis`` (0xC904): rescaled coolant bytes.
    coolant_axis_c904: tuple[int, ...]
    #: ``timing_retard_26d0_rpm_curve`` (0xCC1B), 16 entries.
    curve_cc1b: tuple[int, ...]
    #: ``timing_retard_26d0_bfs_curve`` (0xCC2B), 8 entries.
    curve_cc2b: tuple[int, ...]
    #: ``timing_retard_26d0_coolant_curve`` (0xCC33), 16 entries.
    curve_cc33: tuple[int, ...]
    #: ``timing_retard_17fa_rpm_curve`` (0xD335), 8 entries.
    curve_d335: tuple[int, ...]
    #: ``timing_retard_17fa_bfs_curve`` (0xD33D), 8 entries.
    curve_d33d: tuple[int, ...]


@dataclass(frozen=True)
class BaseTimingTerm:
    """0xFFFF27C6 and every intermediate of its chain."""

    #: ``0xFFFF27C6``, signed degrees.
    term: int
    charge: ChargeTerm
    rate: RateTerm
    #: ``0xFFFF19CB``.
    b19cb: int
    #: ``0xFFFF10EB`` as used.
    b10eb: int
    #: ``0xFFFF11CA`` as used.
    rpm50: int


def timing_term_27c6_chain(
    *,
    rpm_word_11d8: int,
    rpm50_11ca: int,
    x_103e: int,
    coolant_1077: int,
    w1b24: int,
    w1512: int,
    b10eb: int,
    tables: BaseTimingTables,
    cals: BaseTimingCals,
) -> BaseTimingTerm:
    """0x04BA52, 0x04B9D0, 0x04BA6C and 0x04BC5A in order, on the snapshot
    bytes each reads. ``coolant_1077`` is the RESCALED coolant byte
    (:func:`coolant_rescaled`)."""
    b19cb = timing_load_curve_19cb(tables.load_curve_cd9b, x_103e)
    charge = timing_charge_term_17c6(x_103e=x_103e, w1b24=w1b24, b10eb=b10eb, w1512=w1512)
    rate = timing_rate_word_15de(
        coolant_1077=coolant_1077,
        w1b24=w1b24,
        rpm50_11ca=rpm50_11ca,
        x_103e=x_103e,
        rpm_word_11d8=rpm_word_11d8,
        b10eb=b10eb,
        w1512=w1512,
        rate_map_9af9=tables.rate_map_9af9,
        rpm_axis_c890=tables.rpm_axis_c890,
        load_axis_c88c=tables.load_axis_c88c,
        coolant_curve_cebb=tables.coolant_curve_cebb,
        charge_curve_ceab=tables.charge_curve_ceab,
        coolant_curve_d9af=tables.coolant_curve_d9af,
        charge_curve_d60f=tables.charge_curve_d60f,
        cal_8440=cals.cal_8440,
        cal_880e=cals.cal_880e,
    )
    term = timing_term_27c6(
        w17c6=charge.w17c6,
        w15de=rate.w15de,
        b19cb=b19cb,
        rpm_word=rpm_word_11d8,
        cal_880d=cals.cal_880d,
        cal_8b0c=cals.cal_8b0c,
    )
    return BaseTimingTerm(term, charge, rate, b19cb, b10eb, rpm50_11ca)


#: ``0xFFFF11CA`` is ``min(255, accumulator >> 8)`` and the rpm word
#: ``accumulator >> 6``, so in a steady state it is ``min(255, rpm word >> 2)``.
RPM50_FROM_WORD_SHIFT = 2


def timing_term_27c6_steady(
    *,
    rpm_word: int,
    x: int,
    coolant_1077: int,
    w1b24: int,
    w1512: int,
    tables: BaseTimingTables,
    cals: BaseTimingCals,
) -> BaseTimingTerm:
    """:func:`timing_term_27c6_chain` in a STEADY state: rpm and X have held
    for at least a cycle, so the snapshot copies equal their sources and
    ``0xFFFF10EB`` is :func:`timing_charge_map_10eb` at the same rpm word and
    X, and ``0xFFFF11CA = min(255, rpm word >> 2)``. Off steady state pass
    the snapshot bytes to :func:`timing_term_27c6_chain` instead."""
    b10eb = timing_charge_map_10eb(
        tables.charge_map_9b89, tables.rpm_axis_c890, tables.load_axis_c88c, rpm_word, x
    )
    return timing_term_27c6_chain(
        rpm_word_11d8=rpm_word,
        rpm50_11ca=min(BYTE_MAX, _word("rpm word", rpm_word) >> RPM50_FROM_WORD_SHIFT),
        x_103e=x,
        coolant_1077=coolant_1077,
        w1b24=w1b24,
        w1512=w1512,
        b10eb=b10eb,
        tables=tables,
        cals=cals,
    )


#: ``0xFFFF11CE`` is the cycle copy of ``0xFFFF1206 = min(255, 0xFFFF1204 >>
#: 3)`` and the rpm word ``0xFFFF1210 = 0xFFFF1204 >> 3`` (RPM_words_task
#: 0x03907A, 0x038FEA), so in a steady state it is ``min(255, rpm word)``.
def rpm_byte_11ce(rpm_word: int) -> int:
    """The steady-state ``0xFFFF11CE``: ``min(255, rpm word)`` (12.5 rpm per
    count)."""
    return min(BYTE_MAX, _word("rpm word", rpm_word))


def timing_retard_map_3630(
    map_9b09: Sequence[Sequence[int]],
    rpm_axis_c8fc: Sequence[int],
    coolant_axis_c904: Sequence[int],
    rpm_byte_11ce: int,
    coolant_1077: int,
) -> int:
    """``0x04C580``: ``0xFFFF3630`` = map 0x9B09 (8 x 8) through 0x026974 at
    the rpm byte 0xFFFF11CE (rows, axis 0xC8FC) and the RESCALED coolant byte
    0xFFFF1077 (columns, axis 0xC904), both searched directly by 0x027114.
    While 0xFFFF283D bit 7 is set the ECM keeps the last value instead; this
    is the lookup, i.e. that bit clear."""
    return lookup_2d(
        [[_byte("0x9B09 cell", c) for c in row] for row in map_9b09],
        list(rpm_axis_c8fc),
        list(coolant_axis_c904),
        _byte("0xFFFF11CE", rpm_byte_11ce),
        _byte("0xFFFF1077 (rescaled coolant)", coolant_1077),
    ).value


def timing_term_26d9(b3630: int, b15e5: int) -> int:
    """``0x04C580``: ``0xFFFF26D9 = 0xFFFF3630 x 0xFFFF15E5 >> 8``. 0xFFFF3630
    is map 0x9B09 (8 x 8, through 0x026974) at (the rpm byte 0xFFFF11CE, the
    rescaled coolant byte 0xFFFF1077) unless 0xFFFF283D bit 7 holds the last
    value; 0xFFFF15E5 is a 0..255 fade ramp (0x04C49E). 0x9B09 is all zero on
    every listed image, so 0xFFFF26D9 is 0 there."""
    return (_byte("0xFFFF3630", b3630) * _byte("0xFFFF15E5", b15e5)) >> TERM_26D9_SHIFT


@dataclass(frozen=True)
class Term26d0Curves:
    """``0x04C7A0``'s three curve reads."""

    #: ``0xFFFF1301``: 16-entry curve 0xCC1B (0x026BBC) at the rpm/25 byte 0xFFFF11CF.
    b1301: int
    #: ``0xFFFF1302``: 8-entry curve 0xCC2B (0x026BA4) at ``min(255, w1038 >> 7)``.
    b1302: int
    #: ``0xFFFF1303``: 16-entry curve 0xCC33 (0x026BBC) at the rescaled coolant byte.
    b1303: int


def timing_26d0_curves(
    *,
    curve_cc1b: Sequence[int],
    curve_cc2b: Sequence[int],
    curve_cc33: Sequence[int],
    rpm25_11cf: int,
    w1038: int,
    coolant_1077: int,
) -> Term26d0Curves:
    """``0x04C7A0`` (listing 0x04C7C8-0x04C7F2): 0xFFFF1302 = 8-entry curve
    0xCC2B at ``min(255, 0xFFFF1038 >> 7)`` (the previous BFS word); 0xFFFF1301
    = 16-entry curve 0xCC1B at the rpm/25 byte 0xFFFF11CF; 0xFFFF1303 =
    16-entry curve 0xCC33 at the rescaled coolant byte 0xFFFF1077. 0xCC33 is
    0 from entry 10 on every listed image, so 0xFFFF1303 is 0 once the
    rescaled coolant byte reaches 160 (60 degC)."""
    bfs = min(BYTE_MAX, _word("0xFFFF1038", w1038) >> BFS_CURVE_SHIFT)
    return Term26d0Curves(
        b1301=byte_curve16(curve_cc1b, rpm25_11cf),
        b1302=byte_curve8(curve_cc2b, bfs),
        b1303=byte_curve16(curve_cc33, coolant_1077),
    )


def timing_term_26d0(b1300: int, b1301: int, b1302: int, b1303: int) -> int:
    """``0x04C7A0``: ``0xFFFF26D0 = 1300 x 1303 x 1301 x 1302 >> 24`` (32-bit
    ``mul.l`` products; four bytes never wrap). 0xFFFF1300 is a 0..255 ramp
    (0x04C5D8, +/-2 per call) the twin takes as an argument."""
    product = (
        _byte("0xFFFF1300", b1300)
        * _byte("0xFFFF1303", b1303)
        * _byte("0xFFFF1301", b1301)
        * _byte("0xFFFF1302", b1302)
    )
    return (product & U32) >> TERM_26D0_SHIFT


def timing_term_279a(b26d9: int, b26d0: int, b17fa: int) -> int:
    """``0x04C952``: ``0xFFFF279A = clamp(0xFFFF26D9 + 0xFFFF26D0 -
    (0xFFFF17FA >> 4), 0, 255)``, subtracted in the base timing sum. With
    0xFFFF26D9 = 0 (map 0x9B09 all zero) and 0xFFFF26D0 = 0 (warm engine) it
    is 0 whatever 0xFFFF17FA holds."""
    total = (
        _byte("0xFFFF26D9", b26d9)
        + _byte("0xFFFF26D0", b26d0)
        - (_byte("0xFFFF17FA", b17fa) >> TERM_17FA_SHIFT)
    )
    return max(0, min(BYTE_MAX, total))


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
    "every chain term but the trim term 0xFFFF2793 and the base term 0xFFFF27C6 is "
    "equal on both ROMs (0xFFFF1D3E, 134A, 174A, 1344, 11CB, 1215, 26D2, 272A, 2334, "
    "46E6, 62B4, 9932): unidentified, never given a value",
    "0xFFFF27C6 is computed per ROM at a steady state (snapshot copies equal their "
    "sources) from that ROM's tables and its own X; 0xFFFF1B24, which it reads, is "
    "never computed (0x4000 + 0xFFFF175C + 0xFFFF1736 - 0xFFFF26E8, times "
    "0xFFFF26C0: 1736 reads X, 175C reads BFS, 26C0 follows the Target AFR lookup), "
    "so it is swept over 0x4000 x [0.7, 1.3]: held equal on both ROMs only where "
    "the change moves none of X, BFS and phi, else swept on each ROM independently "
    "(charge_word_inputs_moved); a change that moves across the band is a range; "
    "0xFFFF1512 = 0 (cal 0x8538 = 0)",
    "0xFFFF279A is 0 on both ROMs where map 0x9B09 and curve 0xCC33 read 0 at the "
    "point (0xFFFF283D bit 7 clear, so 0xFFFF3630 is the lookup), else held equal "
    "where neither BFS nor its tables change",
    "trim path: 0xFFFF2833 bit 4 and 0xFFFF4785 bit 6 clear (else 0xFFFF27C3 or "
    "0xFFFF1A59 replaces the trim sum), no 0xFFFF28B4 bit 1 bypass",
    "rate limit at its fixed point with a nonzero rise allowance; final byte on "
    "the 110 - 0xFFFF232E branch, 0xFFFF4858 bit 5 clear",
    "intermediate signed-byte clamps (0xFFFF1A58, 0xFFFF1346) not binding",
)

TimingStatus = Literal["computed", "delta_if_no_clamp", "range", "unevaluable"]


@dataclass(frozen=True)
class TimingDelta:
    """Base and candidate ignition timing at one point, degrees BTDC.

    ``range``: the change is known only to lie in ``[delta_min_deg,
    delta_max_deg]`` (a held term swept over a band moves it); ``delta_deg``
    and ``candidate_deg`` are then ``None`` and the ends carry the answer. For
    an exact answer both ends equal ``delta_deg``."""

    status: TimingStatus
    #: Candidate minus base, degrees; ``None`` when unevaluable or a range.
    delta_deg: int | None
    #: The trim term's own change (0xFFFF2793), counts; set whenever both
    #: selected trims are known, even where ``delta_deg`` cannot be.
    trim_term_delta: int | None
    base_deg: int | None
    candidate_deg: int | None
    reasons: tuple[str, ...]
    #: The change's ends, clamps applied when the base is known; ``None``
    #: when unevaluable.
    delta_min_deg: int | None = None
    delta_max_deg: int | None = None
    #: The candidate's absolute ends (only with a known base).
    candidate_min_deg: int | None = None
    candidate_max_deg: int | None = None


def timing_delta(
    base: Step[TrimLookup],
    candidate: Step[TrimLookup],
    *,
    base_timing_deg: int | None = None,
    held_reasons: Sequence[str] = (),
    other_delta: tuple[int, int] = (0, 0),
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
    - ``held_reasons`` are what the caller knows moves a held term it cannot
      compute (a Target AFR change moves 0xFFFF174A): any one leaves the
      change unevaluable, the trim term's change still reported.
    - ``other_delta`` is the ``(min, max)`` change, degrees, of the base-sum
      terms the caller DID compute (0xFFFF27C6 across a swept 0xFFFF1B24),
      added to the trim term's change. Unequal ends give status ``range``:
      both ends go through the same clamps, and ends that clamp to one value
      are exact again."""
    lo_other, hi_other = (int(v) for v in other_delta)
    if lo_other > hi_other:
        raise ValueError(f"other_delta {other_delta!r} is not (min, max)")
    if base.value is None or candidate.value is None:
        reasons = tuple(dict.fromkeys(r for r in (base.reason, candidate.reason) if r))
        return TimingDelta("unevaluable", None, None, None, None, reasons)
    tb, tc = base.value.trim, candidate.value.trim
    if min(tb, tc) < TRIM_TERM_SUBTRAHEND_MAX:
        return TimingDelta("unevaluable", None, None, None, None, ("trim_term_floor_may_bind",))
    d = tc - tb
    if held_reasons:
        return TimingDelta("unevaluable", None, d, None, None, tuple(held_reasons))
    lo, hi = d + lo_other, d + hi_other
    if base_timing_deg is None:
        if lo == hi:
            reasons = ("clamp_headroom_unknown",) if lo else ()
            return TimingDelta("delta_if_no_clamp", lo, d, None, None, reasons, lo, hi)
        return TimingDelta("range", None, d, None, None, ("clamp_headroom_unknown",), lo, hi)
    b = int(base_timing_deg)
    if not TIMING_FLOOR < b < TIMING_CAP:
        return TimingDelta("unevaluable", None, d, b, None, ("base_timing_at_clamp",))
    c_lo = max(TIMING_FLOOR, min(TIMING_CAP, b + lo))
    c_hi = max(TIMING_FLOOR, min(TIMING_CAP, b + hi))
    clamped = [c for c, raw in ((c_lo, b + lo), (c_hi, b + hi)) if c != raw]
    bound = [
        name
        for name, at in (("timing_cap_binds", TIMING_CAP), ("timing_floor_binds", TIMING_FLOOR))
        if at in clamped
    ]
    if c_lo == c_hi:
        return TimingDelta(
            "computed", c_lo - b, d, b, c_lo, tuple(bound), c_lo - b, c_lo - b, c_lo, c_lo
        )
    return TimingDelta("range", None, d, b, None, tuple(bound), c_lo - b, c_hi - b, c_lo, c_hi)


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
    "BaseTimingCals",
    "BaseTimingTables",
    "BaseTimingTerm",
    "ChargeTerm",
    "Checkpoint",
    "EnrichmentCycle",
    "EnrichmentInputs",
    "EnrichmentLatches",
    "EnrichmentState",
    "FinalBranch",
    "FlagRead",
    "PedalFlagModel",
    "RateTerm",
    "RetardCombine",
    "SelectedTrim",
    "Step",
    "Term26d0Curves",
    "Term279aTables",
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
    "byte_curve8",
    "coolant_rescaled",
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
    "timing_26d0_curves",
    "timing_charge_map_10eb",
    "timing_charge_term_17c6",
    "timing_charge_word_1b24",
    "timing_cycle_copy",
    "timing_degrees_btdc",
    "timing_delta",
    "timing_final_byte",
    "timing_load_curve_19cb",
    "timing_rate_limit",
    "timing_rate_limit_steady",
    "timing_rate_word_15de",
    "rpm_byte_11ce",
    "timing_retard_combine",
    "timing_retard_map_3630",
    "timing_term_1512",
    "timing_term_26d0",
    "timing_term_26d9",
    "timing_term_279a",
    "timing_term_27c6",
    "timing_term_27c6_chain",
    "timing_term_27c6_steady",
    "timing_trim_terms",
    "trim_lookup",
    "uprev_power_on",
    "uprev_step",
]

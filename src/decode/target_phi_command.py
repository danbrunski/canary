"""The target-equivalence-ratio command from raw values, once (E02, E06.1).

The pure part of the target-phi twin: given the resolved table (cells and
breakpoints in each input's comparison domain), one tuple of raw lookup
inputs and the decoded gate constants, the branch, the lookup and the gated
command word. No profile, session, pairing, pydantic or I/O: everything is a
value the caller supplies, so the stored evaluator, the live stream and any
other implementation of ``docs/ecu-twin-computation-contract.md`` (the Rust
trial, E06.2) compute the same thing from the same numbers.

:func:`src.analysis.target_phi.predict` is the adapter that unpacks a
``TwinDefinition``/``ResolvedTable`` into these arguments; the stored and live
twins both reach the kernel through it.

**Branch.** Bit ``flag_bit`` of ``flag_raw`` forces the load input to the
all-ones value of a ``load_bits`` input (the firmware's ``0xFF``), which the
axis search clamps to the last column: ``override``. Otherwise the load input
is ``load_raw``: ``ordinary``.

**Gate** (``0x055E5C``-``0x055EEC``). A lookup ``>= threshold`` passes
unchanged; one in ``[threshold - hysteresis, threshold)`` depends on a latch
bit the capture does not carry, so it has no word and names
``enrichment_gate_state_unknown``; anything below writes ``floor``. The word
is the gated cell shifted left by ``cell_to_word_shift``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from src.decode.ecm_lookup import WORD_MAX, LookupResult, lookup_2d

Branch = Literal["ordinary", "override"]

#: The reason a lookup inside the gate's hysteresis band carries.
GATE_STATE_UNKNOWN = "enrichment_gate_state_unknown"

#: Load inputs the forced column can stand for: the all-ones value must be a
#: coordinate the 16-bit axis search can be given.
FORCED_LOAD_BITS = (8, 16)
#: The forced-column flag is read from a word of at most 32 bits.
MAX_FLAG_BIT = 31
FLAG_MAX = 0xFFFFFFFF
#: Largest left shift from a cell to the command word (a 16-bit cell shifted
#: 16 still fits the 32-bit word the contract declares).
MAX_CELL_TO_WORD_SHIFT = 16


@dataclass(frozen=True)
class GateConstants:
    """The decoded enrichment gate, as raw cells."""

    floor_cell: int
    threshold_cell: int
    hysteresis_cells: int


@dataclass(frozen=True)
class CommandResult:
    branch: Branch
    lookup: LookupResult
    #: The lookup value (a cell, or an interpolation of cells).
    cell: int
    #: ``None`` inside the gate's hysteresis band.
    word: int | None
    reason: str | None


def target_command(
    cells: Sequence[Sequence[int]],
    row_breakpoints: Sequence[int],
    column_breakpoints: Sequence[int],
    *,
    rpm_raw: int,
    load_raw: int,
    flag_raw: int,
    flag_bit: int,
    load_bits: int,
    gate: GateConstants,
    cell_to_word_shift: int,
) -> CommandResult:
    """One tuple of raw lookup inputs to the gated command (module docstring).

    Rows are the rpm input, columns the load input. Refuses (``ValueError``)
    a load width whose all-ones value is not a 16-bit coordinate and a shift
    past :data:`MAX_CELL_TO_WORD_SHIFT`; the lookup refuses non-word
    coordinates and a table that does not fit its axes.
    """
    if not 0 <= flag_bit <= MAX_FLAG_BIT:
        raise ValueError(f"flag bit {flag_bit} is outside a 32-bit flag word")
    if load_bits not in FORCED_LOAD_BITS:
        raise ValueError(f"load input of {load_bits} bits is outside a 16-bit word")
    if not 0 <= cell_to_word_shift <= MAX_CELL_TO_WORD_SHIFT:
        raise ValueError(f"cell_to_word_shift {cell_to_word_shift} is outside a 16-bit word")
    flag = _whole("flag_raw", flag_raw, FLAG_MAX)
    forced = bool((flag >> flag_bit) & 1)
    load = (1 << load_bits) - 1 if forced else _whole("load_raw", load_raw, WORD_MAX)
    rpm = _whole("rpm_raw", rpm_raw, WORD_MAX)
    result = lookup_2d(cells, row_breakpoints, column_breakpoints, rpm, load)
    branch: Branch = "override" if forced else "ordinary"
    cell = result.value
    if gate.threshold_cell - gate.hysteresis_cells <= cell < gate.threshold_cell:
        return CommandResult(branch, result, cell, None, GATE_STATE_UNKNOWN)
    gated = cell if cell >= gate.threshold_cell else gate.floor_cell
    return CommandResult(branch, result, cell, gated << cell_to_word_shift, None)


def x_from_bfs(bfs: int, scaler: int, *, shift: int, cap: int) -> int:
    """The Target AFR load input from the BFS word: ``min(cap, (bfs << shift)
    // scaler)``, an unsigned truncating divide. A zero scaler is a
    ``ZeroDivisionError``: no calibration the program reads holds one.

    The firmware's BFS is a 16-bit word; this reference also accepts a wider
    non-negative value, because counterfactual callers model a BFS the ECM
    has not produced (``fuel_comp_knock_on``). Where the ECM would saturate
    such a BFS is not decoded (contract blocker ``bfs_saturation``). Neither
    input is ever truncated: a fraction or a negative BFS is refused."""
    if not 0 <= shift <= MAX_CELL_TO_WORD_SHIFT:
        raise ValueError(f"shift {shift} is outside a 16-bit word")
    whole_bfs = _whole("bfs", bfs, None)
    whole_scaler = _whole("scaler", scaler, WORD_MAX)
    return min(cap, (whole_bfs << shift) // whole_scaler)


def _whole(name: str, value: float, maximum: int | None) -> int:
    """``value`` as a whole non-negative count, never truncated: a fraction is
    ``not a whole raw count``, a value past ``maximum`` (or negative) is
    outside its word. A whole float (a stored raw count) is accepted."""
    if int(value) != value:
        raise ValueError(f"{name} {value!r} is not a whole raw count")
    if value < 0 or (maximum is not None and value > maximum):
        width = "a 32-bit flag word" if maximum == FLAG_MAX else "a 16-bit word"
        raise ValueError(f"{name} {value!r} is outside {width}")
    return int(value)


__all__ = [
    "FLAG_MAX",
    "FORCED_LOAD_BITS",
    "GATE_STATE_UNKNOWN",
    "MAX_CELL_TO_WORD_SHIFT",
    "MAX_FLAG_BIT",
    "Branch",
    "CommandResult",
    "GateConstants",
    "target_command",
    "x_from_bfs",
]

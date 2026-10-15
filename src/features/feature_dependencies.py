"""What each channel, derived channel and behavior model reads.

The dependency traversal the perturbation fast path uses to decide which
stages a changed column can reach.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from src.columns import FLAG_SUFFIXES, GAP_SUFFIX, MODE_COLUMN, base_channel
from src.features.feature_transform import segmenter_input_columns
from src.preprocessing.drives import DRIVE_COLUMN
from src.signals.committees import committee_channel_reason


def column_root(column: str) -> str:
    """The channel a column belongs to: rolling and bookkeeping suffixes off
    (``obd:MAF__roc`` and ``obd:MAF__gap`` are both ``obd:MAF``)."""
    root = base_channel(column)
    for suffix in FLAG_SUFFIXES:
        root = root.removesuffix(suffix)
    return root


def behavior_model_input_roots(model: Any) -> frozenset[str]:
    """Every channel a behavior model's definition names, as column roots.

    Read generically from the whole model (axes, output, eligibility gates,
    cold-start channels, local ranges, required context), so a field added to
    the schema is covered without this list being edited. Any string holding a
    source separator counts; a timestamp string is harmlessly over-included.
    """
    from src.behavior.behavior_eligibility import resolve_output_column

    held = _MODEL_ROOTS.get(id(model))
    if held is not None and held[0] is model:
        return held[1]
    roots: set[str] = {column_root(resolve_output_column(model.output))}

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if ":" in value:
                roots.add(column_root(value))
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, list | tuple | set | frozenset):
            for item in value:
                walk(item)

    walk(model.model_dump())
    if len(_MODEL_ROOTS) > 1024:
        _MODEL_ROOTS.clear()
    result = frozenset(roots)
    # Held with the model itself, so a reused id can never serve another's.
    _MODEL_ROOTS[id(model)] = (model, result)
    return result


#: :func:`behavior_model_input_roots` by model (a model's dump is ~2 ms, and
#: the injection sweep asks for every model on every injection).
_MODEL_ROOTS: dict[int, tuple[Any, frozenset[str]]] = {}

#: Columns every stage may read beside its own inputs: the row structure.
ROW_COLUMNS = ("session", "timestamp", MODE_COLUMN, DRIVE_COLUMN)


def segmenter_read_columns() -> frozenset[str]:
    """Every column the operating-mode segmenter reads: its candidate signals,
    their gap flags (stale-gated signals) and the row structure."""
    inputs = segmenter_input_columns()
    return frozenset({*inputs, *(f"{column}{GAP_SUFFIX}" for column in inputs), *ROW_COLUMNS})


def derived_channel_inputs(channel: str) -> frozenset[str]:
    """Every channel a ``derived:`` channel's computation may read: each member
    of each declared input group (any may be the one chosen) plus its
    optional refinements. Empty for a channel that is not derived."""
    from src.signals.derived import DERIVED_CHANNEL_INPUTS, DERIVED_CHANNEL_OPTIONAL_INPUTS

    groups = DERIVED_CHANNEL_INPUTS.get(channel, ())
    return frozenset(
        {member for group in groups for member in group}
        | set(DERIVED_CHANNEL_OPTIONAL_INPUTS.get(channel, ()))
    )


def derived_channels_reached(roots: Collection[str]) -> frozenset[str]:
    """The derived *feature* channels whose value can depend on any of
    ``roots``, through the declared inputs, transitively.

    Sensor-committee outputs are left out: they are detectors, never features
    (H04.1), and their member transforms are not declared inputs.
    """
    from src.signals.derived import DERIVED_CHANNEL_INPUTS

    features = [c for c in DERIVED_CHANNEL_INPUTS if committee_channel_reason(c) is None]
    reached: set[str] = set()
    frontier = set(roots)
    while True:
        new = {
            channel
            for channel in features
            if channel not in reached and derived_channel_inputs(channel) & (frontier | reached)
        }
        if not new:
            return frozenset(reached)
        reached |= new
        frontier |= new


def derived_ancestors(channels: Collection[str]) -> frozenset[str]:
    """``channels`` and every input they read, transitively."""
    seen: set[str] = set()
    stack = list(channels)
    while stack:
        channel = stack.pop()
        if channel in seen:
            continue
        seen.add(channel)
        stack.extend(derived_channel_inputs(channel) - seen)
    return frozenset(seen)

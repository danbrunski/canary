"""Putting :func:`~src.analysis.channels.read_channels` frames on one clock.

The second half of :mod:`src.analysis.channels` (R26.8): the held-value rule for
change-only broadcast storage (:data:`AS_OF_ATTR`, :func:`as_of_validity`,
:func:`as_of_refusal`, :func:`refuse_as_of`, :func:`concat_channel_frames`) and
the cell-based aligned frame (:func:`align_long`). Pure frame code: nothing here
reads a session. ``channels`` imports this module and re-exports every public
name, and keeps :func:`~src.analysis.channels.read_aligned`, the one function
that both reads and aligns, because it calls ``read_channels``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

import pandas as pd

from src.analysis.channels_plan import ChannelSpec, parse_spec
from src.storage.broadcast_storage import (
    FRAME_ATTR,
    ChangeOnlyStorageError,
    change_only_refusal,
    change_only_timeout,
)

#: ``DataFrame.attrs`` key on :func:`read_channels`' output (P02.10): spec ->
#: seconds its rows stay valid after they were stored, for every broadcast spec
#: (``decoded:``/``can:``) read from a session that stores CAN frames
#: change-only. There a row is stored only when the frame's payload changes,
#: and an unchanged id is re-written before this many seconds, so the value at
#: an instant is the last stored row before it, valid this long. Absent (or
#: empty) for a session stored every frame, whose rows are read as they were.
#: :func:`align_long` and :func:`src.analysis.compare.align_pairs` read it, so
#: callers never apply the rule themselves.
AS_OF_ATTR = "as_of_valid_seconds"


#: Spec namespaces decoded from broadcast frames.
_BROADCAST_SPEC_SOURCES = frozenset({"decoded", "can"})


def _as_of_validity(frame: pd.DataFrame, parsed: list[ChannelSpec]) -> dict[str, float]:
    """:data:`AS_OF_ATTR` for ``parsed``: each broadcast spec's validity when
    the session stores CAN frames change-only, from the loader's attrs."""
    timeout = change_only_timeout(frame)
    if timeout is None:
        return {}
    return {spec.text: timeout for spec in parsed if spec.source in _BROADCAST_SPEC_SOURCES}


def concat_channel_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate :func:`read_channels` frames, keeping :data:`AS_OF_ATTR`.

    ``pd.concat`` drops ``attrs`` that differ between its inputs, and two
    reads of different specs always differ, so a plain concat would read a
    change-only broadcast channel as sparse samples (P02.10)."""
    out = pd.concat(frames, ignore_index=True)
    merged: dict[str, float] = {}
    for frame in frames:
        merged.update(as_of_validity(frame))
    out.attrs.pop(AS_OF_ATTR, None)
    if merged:
        out.attrs[AS_OF_ATTR] = merged
    return out


def as_of_validity(long: pd.DataFrame) -> dict[str, float]:
    """:data:`AS_OF_ATTR` of a :func:`read_channels` frame: spec -> seconds."""
    raw = long.attrs.get(AS_OF_ATTR) or {}
    return {str(spec): float(seconds) for spec, seconds in raw.items()}


def as_of_refusal(
    long: pd.DataFrame, *, reader: str, specs: Iterable[str] | None = None
) -> str | None:
    """Why ``reader`` may not treat ``long``'s rows as samples, or ``None``.

    The one statement of P02.10's rule for a :func:`read_channels` caller
    that pairs stored rows one-to-one, counts them, bins them by second, or
    reads rates, gaps or steadiness from them: a broadcast spec read from a
    change-only session (``attrs[AS_OF_ATTR]``) holds its payload changes and
    re-writes, not one row per frame, so each of those answers would be about
    how often the payload changed. ``specs`` narrows the check to the specs
    the caller uses that way (default: every spec with rows in the frame;
    a slice of one spec keeps its read's attrs for all of them). A caller
    that needs the value at an instant uses the held path instead
    (:func:`align_long` / :func:`read_aligned`, ``compare.align_pairs``).
    """
    held = as_of_validity(long)
    if specs is not None:
        wanted = {parse_spec(text).text for text in specs}
        held = {spec: seconds for spec, seconds in held.items() if spec in wanted}
    if held and "spec" in long.columns:
        # A slice of a multi-spec read keeps the whole frame's attrs; only
        # the specs whose rows are present are this caller's.
        present = set(long["spec"].astype(str).unique())
        held = {spec: seconds for spec, seconds in held.items() if spec in present}
    if not held:
        return None
    return (
        change_only_refusal(
            f"{reader} ({', '.join(sorted(held))})",
            _change_only_sessions(long),
            max(held.values()),
        )
        + " A read of such a spec holds its payload changes and re-writes, one row per "
        "change rather than per frame, so pairing its rows one-to-one, counting them or "
        "binning them by second measures how often the payload changed; the held readers "
        "(read_aligned, align_long, compare.align_pairs) give the value as of each instant."
    )


def refuse_as_of(long: pd.DataFrame, *, reader: str, specs: Iterable[str] | None = None) -> None:
    """Raise :class:`~src.storage.broadcast_storage.ChangeOnlyStorageError`
    when :func:`as_of_refusal` names a cause (P02.10)."""
    message = as_of_refusal(long, reader=reader, specs=specs)
    if message is not None:
        raise ChangeOnlyStorageError(message, reader=reader, sessions=_change_only_sessions(long))


def _change_only_sessions(long: pd.DataFrame) -> list[str]:
    info = long.attrs.get(FRAME_ATTR)
    names = list(info.get("change_only_sessions") or []) if isinstance(info, dict) else []
    return names or ["this session"]


def align_long(
    long: pd.DataFrame,
    specs: list[str],
    *,
    grid: str = "1s",
    require_all: bool = True,
    cell_agg: Literal["last", "mean"] = "last",
    max_staleness: pd.Timedelta | str | None = None,
    with_age: bool = False,
) -> pd.DataFrame:
    """:func:`read_aligned`'s alignment step over an already-read long frame.

    Split out so a caller that also needs :func:`read_channels`' per-row
    ``unit`` (the workbench log frame) reads the session once rather than
    twice. Same freshness contract as :func:`read_aligned`.

    A spec :func:`read_channels` marked as-of (``attrs[AS_OF_ATTR]``, a
    change-only broadcast channel, P02.10) is carried from each stored row
    into the following cells for its validity, whatever ``max_staleness``
    says: in that storage no row means "the same payload again", so the cell
    a session stored every frame would fill from a frame of its own is filled
    with the value that frame carried. ``__age_s`` still says how old the
    stored row is. ``cell_agg="mean"`` is refused for such a spec, because a
    mean over the stored changes is not a mean over the frames the bus sent.
    """
    names = [parse_spec(text).text for text in specs]
    if long.empty:
        return pd.DataFrame(columns=["timestamp", *names])

    held = {name: seconds for name, seconds in as_of_validity(long).items() if name in names}
    if held and cell_agg == "mean":
        raise ValueError(
            f"cell_agg='mean' over {sorted(held)}: this session stores CAN frames "
            "change-only (P02.10), so a mean over the stored rows averages payload "
            "changes, not the frames the bus sent; use cell_agg='last'"
        )
    tolerance = pd.Timedelta(max_staleness) if max_staleness is not None else None
    emit_age = with_age or tolerance is not None

    long = long.copy()
    long["bucket"] = long["timestamp"].dt.floor(grid)
    buckets = pd.DatetimeIndex(sorted(long["bucket"].unique()), name="timestamp")
    if held:
        buckets = _with_held_buckets(long, buckets, held, pd.Timedelta(grid))
    out = pd.DataFrame(index=buckets)

    for name in names:
        rows = long[long["spec"] == name]
        cell = rows.groupby("bucket", sort=True).agg(value=("value", cell_agg))
        series = cell["value"].reindex(buckets)
        seen = pd.Series(cell.index, index=cell.index).reindex(buckets)
        spec_tolerance = tolerance
        if name in held:
            validity = pd.Timedelta(seconds=held[name])
            spec_tolerance = validity if tolerance is None else max(tolerance, validity)
        if spec_tolerance is not None:
            series = series.ffill()
            seen = seen.ffill()
        age = (
            buckets.to_series().reset_index(drop=True).to_numpy() - seen.to_numpy()
        ) / pd.Timedelta(seconds=1)
        age_series = pd.Series(age, index=buckets, dtype="float64")
        if spec_tolerance is not None:
            too_old = age_series > spec_tolerance.total_seconds()
            series = series.mask(too_old)
            age_series = age_series.mask(too_old)
        out[name] = series
        if emit_age:
            out[f"{name}__age_s"] = age_series

    if require_all:
        out = out.dropna(subset=names)
    else:
        out = out.dropna(subset=names, how="all")
    return out.reset_index()


def _with_held_buckets(
    long: pd.DataFrame,
    buckets: pd.DatetimeIndex,
    held: dict[str, float],
    step: pd.Timedelta,
) -> pd.DatetimeIndex:
    """``buckets`` plus every cell an as-of spec's stored rows hold (P02.10).

    A session stored every frame has a row of a transmitting id in every
    cell; a change-only one has a row only where the payload changed or was
    re-written. Each stored row therefore stands for the cells after its own,
    up to its validity, clipped to the frame's last cell (nothing is held past
    the end of what was read).
    """
    last = buckets.max()
    extra: list[pd.DatetimeIndex] = []
    for name, seconds in held.items():
        starts = pd.DatetimeIndex(long.loc[long["spec"] == name, "bucket"].unique())
        if starts.empty:
            continue
        steps = int(pd.Timedelta(seconds=seconds) // step)
        for k in range(1, steps + 1):
            shifted = starts + k * step
            extra.append(shifted[shifted <= last])
    if not extra:
        return buckets
    merged = buckets.append(extra).unique().sort_values()
    return pd.DatetimeIndex(merged, name="timestamp")

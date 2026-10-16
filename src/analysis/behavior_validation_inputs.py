"""Behavior-validation inputs: which rows are evidence, and per-read pairing (R20).

Two input contracts, both applied before any row is graded:

* **Default-profile evidence only** (B05.3; R03.12, operator 2026-09-24/25):
  the per-row :data:`PROFILE_EXCLUSION_COLUMN` stamp and the refusal of an
  unstamped capture or tuning piece (:class:`UnverifiedProfileError`).
* **Per-read axis pairing** (B04.9): one row per output read with its axes
  paired on the acquisition clock (:func:`per_read_frame`), the policy stamped
  on every row, and the named refusals of :class:`AxisPairingRefusedError`.

Imports nothing else of the family (``tests/test_behavior_validation_boundaries.py``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.analysis.channels import ChannelSpecError, parse_spec, read_channels
from src.analysis.splits import parent_session
from src.behavior.behavior_models import AxisPairing, BehaviorModel1D, BehaviorModel2D
from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN, gap_column
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.session import SessionKind, session_kind
from src.signals.pairing import PairingPolicy, match_ordered, to_seconds
from src.storage.corpus_eligibility import default_profile_exclusion

# ---------------------------------------------------------------------------
# Default-profile evidence only (B05.3; R03.12, operator 2026-09-24/25)
# ---------------------------------------------------------------------------


#: Per-row stamp naming why a row's session is not behavior-model evidence
#: (``None``: it is). Written by :func:`stamp_profile_exclusions` from the
#: session directories themselves; read by :func:`evaluate_rows` and the refit.
PROFILE_EXCLUSION_COLUMN = "context:profile_exclusion"


#: Refusal code prefix for a row whose session is not the default profile.
PROFILE_EXCLUDED = "profile_excluded"


#: Why a behavior fit or its evidence refused a frame that carries capture or
#: tuning pieces with no :data:`PROFILE_EXCLUSION_COLUMN` verdict.
PROFILE_UNVERIFIED = "profile_unverified"


_PROFILE_SCOPED_KINDS = frozenset({SessionKind.CAPTURE, SessionKind.TUNING})


#: Which recordings count as evidence: ``"default_profile"`` (every caller) or
#: ``"every_recording"`` (a historical reproduction only).
ProfileScope = Literal["default_profile", "every_recording"]


class UnverifiedProfileError(ValueError):
    """Capture or tuning pieces reached behavior evidence without a verdict."""


def profile_exclusions(sessions: Sequence[Path]) -> dict[str, str]:
    """``{session name: reason}`` for the sessions that are not behavior evidence.

    The operator's standing rule (2026-09-24/25; R03.12(a)/(b)) is that
    tuning captures never train, evaluate or score the IF, the sequence model
    or the behavior deviations. The decision per directory is R04.6's
    :func:`src.storage.corpus_eligibility.default_profile_exclusion`, reused rather
    than restated: a ``_tuning-*`` session, or a capture whose own manifest
    says it is not corpus-eligible.
    """

    return {
        Path(session).name: reason
        for session in sessions
        if (reason := default_profile_exclusion(Path(session))) is not None
    }


def stamp_profile_exclusions(frame: pd.DataFrame, sessions: Sequence[Path]) -> pd.DataFrame:
    """``frame`` with :data:`PROFILE_EXCLUSION_COLUMN` set from ``sessions``.

    Excluded rows stay in the frame so drive grouping still sees the capture
    that bridged two parts of one drive (a held-out fold stays honest), but
    they are refused wherever evidence is counted. A capture or tuning piece
    whose directory is not among ``sessions`` cannot be checked and is
    refused too, never assumed eligible.
    """
    if SESSION_COLUMN not in frame.columns:
        raise UnverifiedProfileError(
            f"{PROFILE_UNVERIFIED}: no {SESSION_COLUMN} column to attribute rows to sessions"
        )
    names = {Path(session).name for session in sessions}
    excluded = profile_exclusions(sessions)
    codes: dict[str, str | None] = {}
    for parent in pd.unique(frame[SESSION_COLUMN].astype(str).map(parent_session)):
        if parent in excluded:
            codes[parent] = f"{PROFILE_EXCLUDED}:not_default_profile"
        elif parent not in names and session_kind(parent) in _PROFILE_SCOPED_KINDS:
            codes[parent] = f"{PROFILE_EXCLUDED}:no_session_directory"
        else:
            codes[parent] = None
    parents = frame[SESSION_COLUMN].astype(str).map(parent_session)
    column = pd.Series([codes[parent] for parent in parents], index=frame.index, dtype=object)
    return frame.assign(**{PROFILE_EXCLUSION_COLUMN: column})


def profile_refusals(frame: pd.DataFrame) -> np.ndarray:
    """Per-row profile refusal code (``None``: default-profile evidence).

    Raises :class:`UnverifiedProfileError` when the frame holds a capture or
    tuning piece but was never stamped: silently counting one would be the
    failure this exists to prevent. An unstamped frame of plain sessions has
    nothing to exclude.
    """
    if PROFILE_EXCLUSION_COLUMN in frame.columns:
        values = frame[PROFILE_EXCLUSION_COLUMN].to_numpy(dtype=object)
        return np.asarray([None if pd.isna(v) else str(v) for v in values], dtype=object)
    if SESSION_COLUMN in frame.columns:
        scoped = sorted(
            parent
            for parent in pd.unique(frame[SESSION_COLUMN].astype(str).map(parent_session))
            if session_kind(parent) in _PROFILE_SCOPED_KINDS
        )
        if scoped:
            raise UnverifiedProfileError(
                f"{PROFILE_UNVERIFIED}: capture/tuning piece(s) {', '.join(scoped)} carry no "
                "corpus-eligibility verdict; stamp the frame with stamp_profile_exclusions "
                "(tuning captures are never behavior-model evidence, R03.12)"
            )
    return np.full(len(frame), None, dtype=object)


# ---------------------------------------------------------------------------
# Per-read axis pairing (B04.9)
# ---------------------------------------------------------------------------


#: Per-row refusal from the per-read pairing (``None``: every axis paired).
AXIS_PAIRING_COLUMN = "context:axis_pairing"


#: The pairing a per-read frame was built under, on every row, as JSON
#: ``{"output": spec, "axes": {axis: bound_s}}``. A column rather than
#: ``attrs`` so it survives slicing and concatenation; :func:`evaluate_rows`
#: refuses a frame paired for another model's output or axes.
AXIS_PAIRING_POLICY_COLUMN = "context:axis_pairing_policy"


#: Prefix of the per-axis ``|acquisition separation|`` columns, seconds.
AXIS_SEPARATION_PREFIX = "context:axis_separation_s:"


#: Clocks with no floor under a sub-second tolerance
#: (:func:`src.signals.pairing.basis_floor_s`); a ``round`` or ``unknown``
#: stamp cannot say which read came first inside a round.
_PAIRABLE_BASES = frozenset({"acquired", "stored"})


class AxisPairingRefusedError(ValueError):
    """A per-read pairing that cannot be built or graded; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


def axis_separation_column(axis: str) -> str:
    return f"{AXIS_SEPARATION_PREFIX}{axis}"


def _model_axes(model: BehaviorModel1D | BehaviorModel2D) -> list[str]:
    if isinstance(model, BehaviorModel1D):
        return [model.axis.channel]
    return [axis.channel for axis in model.axes]


def require_axis_pairing_covers(
    model: BehaviorModel1D | BehaviorModel2D, pairing: AxisPairing
) -> None:
    """Refuse a declaration that leaves an axis on the grid's join, or names
    a channel the model does not read."""
    axes = set(_model_axes(model))
    declared = set(pairing.max_separation_s)
    missing = sorted(axes - declared)
    if missing:
        raise AxisPairingRefusedError(
            f"axis_pairing_incomplete:{missing[0]}",
            f"{model.name} reads {sorted(axes)}; axis_pairing bounds only {sorted(declared)}",
        )
    unknown = sorted(declared - axes)
    if unknown:
        raise AxisPairingRefusedError(
            f"axis_pairing_unknown_axis:{unknown[0]}",
            f"{model.name} has no axis {unknown[0]} (axes {sorted(axes)})",
        )


def axis_pairing_bounds(
    frame: pd.DataFrame, model: BehaviorModel1D | BehaviorModel2D
) -> dict[str, float] | None:
    """The per-axis bound ``frame`` was paired under for ``model``, or
    ``None`` for a frame of resampled grid rows."""
    if AXIS_PAIRING_POLICY_COLUMN not in frame.columns:
        if AXIS_PAIRING_COLUMN in frame.columns:
            raise AxisPairingRefusedError(
                "axis_pairing_unstated", "per-read refusals without the policy they came from"
            )
        return None
    column = frame[AXIS_PAIRING_POLICY_COLUMN]
    if column.empty:
        return None
    unstated = int(column.isna().sum())
    if unstated:
        # A grid row concatenated onto a per-read frame has no policy; read
        # past, it would be graded as if its axes had been paired.
        raise AxisPairingRefusedError(
            "axis_pairing_mixed",
            f"{unstated} of {len(column)} rows state no pairing policy (grid rows mixed in)",
        )
    stated = {str(v) for v in column.unique()}
    if len(stated) > 1:
        raise AxisPairingRefusedError(
            "axis_pairing_mixed", f"one frame paired under {len(stated)} policies"
        )
    policy = json.loads(stated.pop())
    if policy["output"] != model.output:
        raise AxisPairingRefusedError(
            "axis_pairing_other_output",
            f"frame was paired on {policy['output']} reads; {model.name} grades {model.output}",
        )
    bounds = {str(axis): float(s) for axis, s in policy["axes"].items()}
    if set(bounds) != set(_model_axes(model)):
        raise AxisPairingRefusedError(
            "axis_pairing_other_axes",
            f"frame was paired on {sorted(bounds)}; {model.name} reads {_model_axes(model)}",
        )
    return bounds


def read_model_reads(
    model: BehaviorModel1D | BehaviorModel2D,
    sessions: Sequence[Path],
    *,
    bindings_path: Path | None = None,
    dids_path: Path | None = None,
) -> dict[str, pd.DataFrame]:
    """Each session's reads of ``model``'s output and axes on the acquisition
    clock (:func:`src.analysis.channels.read_channels`), by directory name.
    Refuses a derived output or axis: it is computed on the grid, not read."""
    specs = [model.output, *_model_axes(model)]
    for spec in specs:
        try:
            parse_spec(spec)
        except ChannelSpecError as exc:
            raise AxisPairingRefusedError(
                f"axis_pairing_not_a_read:{spec}",
                f"{spec} is not read from the bus, so it has no acquisition time ({exc})",
            ) from exc
    return {
        Path(session).name: read_channels(
            Path(session),
            specs,
            acquired_time=True,
            bindings_path=BINDINGS_YAML if bindings_path is None else bindings_path,
            dids_path=DIDS_YAML if dids_path is None else dids_path,
        )
        for session in sessions
    }


def per_read_frame(
    model: BehaviorModel1D | BehaviorModel2D,
    grid: pd.DataFrame,
    reads: Mapping[str, pd.DataFrame],
    pairing: AxisPairing,
    *,
    interval_seconds: float,
) -> pd.DataFrame:
    """One row per read of ``model``'s output, its axes paired per read.

    ``grid`` is the resampled evaluation frame (:func:`build_evaluation_frame`)
    and ``reads`` each session's :func:`read_model_reads`. Each axis read is
    paired at most once with an output read (:func:`match_ordered`, output as
    the reference side) within that axis's bound. Every other column — mode,
    the contract's gates, start context, profile verdict, rates — is the grid
    row of the second the output read was acquired in. The row's timestamp is
    the read's acquisition time.

    Every read gets a row; one that cannot be graded keeps why in
    :data:`AXIS_PAIRING_COLUMN`: ``clock_unsupported:<spec>`` (a round or
    unknown stamp), ``not_captured:<axis>``, ``unpaired:<axis>`` (no partner
    within the bound) or ``no_grid_context`` (the grid has no row for that
    second). A session with no grid row at all contributes nothing.
    """
    require_axis_pairing_covers(model, pairing)
    output = model.output
    axes = _model_axes(model)
    bounds = {axis: float(pairing.max_separation_s[axis]) for axis in axes}
    policy_json = json.dumps({"output": output, "axes": bounds}, sort_keys=True)
    grid_parents = grid[SESSION_COLUMN].astype(str).map(parent_session).to_numpy()
    grid_seconds = to_seconds(grid[TIMESTAMP_COLUMN])
    parts: list[pd.DataFrame] = []
    for parent, long in reads.items():
        positions = np.flatnonzero(grid_parents == parent)
        if not positions.size:
            continue
        positions = positions[np.argsort(grid_seconds[positions], kind="stable")]
        part = _pair_session_reads(
            grid,
            positions,
            grid_seconds[positions],
            long,
            output=output,
            bounds=bounds,
            interval_seconds=interval_seconds,
            policy_json=policy_json,
        )
        if part is not None:
            parts.append(part)
    if not parts:
        empty = grid.iloc[0:0].copy()
        for axis in axes:
            empty[axis_separation_column(axis)] = pd.Series(dtype=float)
        empty[AXIS_PAIRING_COLUMN] = pd.Series(dtype=object)
        empty[AXIS_PAIRING_POLICY_COLUMN] = pd.Series(dtype=object)
        return empty
    out = pd.concat(parts, ignore_index=True)
    return out.sort_values([SESSION_COLUMN, TIMESTAMP_COLUMN], kind="stable").reset_index(drop=True)


def _pair_session_reads(
    grid: pd.DataFrame,
    positions: np.ndarray,
    seconds: np.ndarray,
    long: pd.DataFrame,
    *,
    output: str,
    bounds: Mapping[str, float],
    interval_seconds: float,
    policy_json: str,
) -> pd.DataFrame | None:
    spec_of = long["spec"].astype(str).to_numpy()
    target = long[spec_of == output]
    if target.empty:
        return None
    t = to_seconds(target["timestamp"])
    order = np.argsort(t, kind="stable")
    t = t[order]
    stamps = pd.to_datetime(target["timestamp"], utc=True).iloc[order].reset_index(drop=True)
    values = pd.to_numeric(target["value"], errors="coerce").to_numpy(dtype=float)[order]
    pairable = target["timestamp_basis"].astype(str).isin(_PAIRABLE_BASES).to_numpy()[order]
    reason = np.full(len(t), None, dtype=object)
    reason[~pairable] = f"clock_unsupported:{output}"

    def refuse(mask: np.ndarray, why: str) -> None:
        free = np.asarray([r is None for r in reason], dtype=bool)
        reason[mask & free] = why

    paired: dict[str, np.ndarray] = {}
    separation: dict[str, np.ndarray] = {}
    pairable_rows = np.flatnonzero(pairable)
    for axis, bound in bounds.items():
        captured = long[spec_of == axis]
        side = captured[captured["timestamp_basis"].astype(str).isin(_PAIRABLE_BASES)]
        vals = np.full(len(t), np.nan)
        sep = np.full(len(t), np.nan)
        if side.empty:
            why = f"not_captured:{axis}" if captured.empty else f"clock_unsupported:{axis}"
            refuse(np.ones(len(t), dtype=bool), why)
        else:
            matched = match_ordered(
                t[pairable_rows],
                to_seconds(side["timestamp"]),
                PairingPolicy(
                    max_separation=pd.Timedelta(seconds=bound), reference="a", tie_break="earlier"
                ),
            )
            rows = pairable_rows[matched.index_a]
            side_values = pd.to_numeric(side["value"], errors="coerce").to_numpy(dtype=float)
            vals[rows] = side_values[matched.index_b]
            sep[rows] = matched.separation_s
            refuse(~np.isfinite(sep), f"unpaired:{axis}")
        paired[axis] = vals
        separation[axis] = sep
    # The grid row of the second each read was acquired in: the resampler
    # files a read under the floor of its second.
    second = np.floor(t / interval_seconds) * interval_seconds
    k = np.searchsorted(seconds, second + 1e-6, side="right") - 1
    k_ctx = np.clip(k, 0, len(seconds) - 1)
    has_context = (k >= 0) & (np.abs(seconds[k_ctx] - second) < 1e-6)
    # A read with no row of its own borrows a neighbour's piece and profile
    # verdict (both per session) so it is counted, but it is refused.
    refuse(~has_context, "no_grid_context")
    context = grid.iloc[positions[k_ctx]].reset_index(drop=True)
    updates: dict[str, Any] = {TIMESTAMP_COLUMN: stamps, output: values}
    for channel, fresh in [(output, np.ones(len(t), dtype=bool))] + [
        (axis, np.isfinite(separation[axis])) for axis in bounds
    ]:
        if channel != output:
            updates[channel] = paired[channel]
        if gap_column(channel) in context.columns:
            updates[gap_column(channel)] = ~fresh
    for axis in bounds:
        updates[axis_separation_column(axis)] = separation[axis]
    updates[AXIS_PAIRING_COLUMN] = reason
    updates[AXIS_PAIRING_POLICY_COLUMN] = np.full(len(t), policy_json, dtype=object)
    # One concat, not a column at a time: the grid is hundreds of columns wide.
    kept = context.drop(columns=[c for c in updates if c in context.columns])
    return pd.concat([kept, pd.DataFrame(updates, index=kept.index)], axis=1)


@dataclass(frozen=True)
class SessionReads:
    """Where a grid frame's reads come from (B04.9): the session directories
    it was built from and the catalogs that decode them. A model graded per
    read re-reads its output and axes from these on the acquisition clock."""

    sessions: tuple[Path, ...] = ()
    bindings_path: Path | None = None
    dids_path: Path | None = None


def graded_frame(
    model: BehaviorModel1D | BehaviorModel2D,
    grid: pd.DataFrame,
    pairing: AxisPairing | None,
    reads: SessionReads,
    *,
    interval_seconds: float,
) -> pd.DataFrame:
    """The frame ``model`` is graded on, for the evaluator and the refit alike.

    Without a declared ``pairing``: ``grid`` itself. With one:
    :func:`per_read_frame` over the reads of every session ``grid``'s pieces
    came from. Refuses (``axis_pairing_no_reads``) when no session directory
    is named, or when one of ``grid``'s sessions has none: graded on what is
    left, the missing drive would silently vanish from the evidence.
    """
    if pairing is None:
        return grid
    if not reads.sessions:
        raise AxisPairingRefusedError(
            "axis_pairing_no_reads",
            f"{model.name} is graded per read; name the session directories",
        )
    if SESSION_COLUMN not in grid.columns:
        raise AxisPairingRefusedError(
            "axis_pairing_no_reads", f"{model.name}: the frame names no sessions to read"
        )
    by_name = {Path(session).name: Path(session) for session in reads.sessions}
    parents = sorted({parent_session(p) for p in grid[SESSION_COLUMN].astype(str).unique()})
    missing = [parent for parent in parents if parent not in by_name]
    if missing:
        raise AxisPairingRefusedError(
            "axis_pairing_no_reads",
            f"{model.name} is graded per read; no session directory for {', '.join(missing)}",
        )
    return per_read_frame(
        model,
        grid,
        read_model_reads(
            model,
            [by_name[parent] for parent in parents],
            bindings_path=reads.bindings_path,
            dids_path=reads.dids_path,
        ),
        pairing,
        interval_seconds=interval_seconds,
    )

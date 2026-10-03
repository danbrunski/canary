"""Per-recipe input inventory over recorded sessions (B05.1).

Before any recipe is fitted, count what the corpus can actually support it
with — through the same serving contract the fit and the daemon would apply,
so "viable rows" means rows the recipe would really use, not rows where a
column happens to be non-empty. Kept apart, because they demand different
responses:

* **absent** — the channel was never recorded in the session (a capture
  profile that does not poll it). No amount of fitting fixes that.
* **stale** — recorded, but not fresh on the row (the resampler's gap flag).
* **ineligible** — fresh inputs, refused by the contract (engine off, open
  loop, excluded mode, rate gate …), with the refusal named.
* **eligible** — what a fit could use.

For the fuel-trim and bank-balance recipes the per-bank facts are reported
separately: a bank whose trims are missing is *missing-bank coverage*, never a
zero bank difference, and the ECM's second fuel-system state (PID 0x03 byte
B, bank 2 on this engine layout) is compared with the first when raw
payloads are available (:func:`fuel_system_state_agreement`).

Nothing here fits, publishes or reads a model artifact.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.refit import RPM_COLUMN, Recipe, recipe_serving_spec
from src.behavior.behavior_eligibility import (
    eligibility_reasons,
    reason_code,
    required_channel_groups,
)
from src.behavior.session_context import (
    StartContextSpec,
    append_start_context,
    start_context_columns,
)
from src.columns import MODE_COLUMN, ROC_SUFFIX, gap_column
from src.dataframe_utils import numeric_values
from src.operating import CLOSED_LOOP_FUEL_STATES, ENGINE_RUNNING_MIN_RPM, fuel_status_name
from src.preprocessing.features import compute_rate_of_change

#: Per-bank trim channels (J1979 PIDs 0x06-0x09).
TRIM_BANKS: Mapping[str, tuple[str, str]] = {
    "b1": ("obd:SHORT_FUEL_TRIM_1", "obd:LONG_FUEL_TRIM_1"),
    "b2": ("obd:SHORT_FUEL_TRIM_2", "obd:LONG_FUEL_TRIM_2"),
}
FUEL_STATUS_COLUMN = "obd:FUEL_STATUS"


def _fresh(frame: pd.DataFrame, column: str) -> np.ndarray:
    fresh = np.isfinite(numeric_values(frame, column))
    gap = gap_column(column)
    if gap in frame.columns:
        fresh &= ~frame[gap].fillna(True).astype(bool).to_numpy()
    return np.asarray(fresh, dtype=bool)


@dataclass(frozen=True)
class ChannelCoverage:
    """One any-of channel group on one frame."""

    group: tuple[str, ...]
    recorded: bool
    present: int
    fresh: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "group": list(self.group),
            "recorded": self.recorded,
            "present": self.present,
            "fresh": self.fresh,
        }


@dataclass(frozen=True)
class RecipeInventory:
    """What one frame offers one recipe, through the recipe's own contract."""

    recipe: str
    rows: int
    running_rows: int
    channels: list[ChannelCoverage]
    eligible: int
    refusals: dict[str, int] = field(default_factory=dict)

    @property
    def absent(self) -> list[str]:
        return ["|".join(c.group) for c in self.channels if not c.recorded]

    def as_dict(self) -> dict[str, Any]:
        return {
            "recipe": self.recipe,
            "rows": self.rows,
            "running_rows": self.running_rows,
            "absent_channels": self.absent,
            "channels": [c.as_dict() for c in self.channels],
            "eligible": self.eligible,
            "refusals": self.refusals,
        }


def with_contract_context(frame: pd.DataFrame, recipes: Iterable[Recipe]) -> pd.DataFrame:
    """Add the derivatives and start context the recipes' contracts read.

    Rate gates need ``<channel>__roc`` from the feature pipeline's single
    rate implementation; cold-start contracts need the engine-start fold.
    Both are computed exactly as the refit and evaluator compute them.
    """
    out = frame
    roc_bases: list[str] = []
    starts: list[StartContextSpec] = []
    for recipe in recipes:
        spec = recipe_serving_spec(recipe)
        roc_bases.extend(c.channel.removesuffix(ROC_SUFFIX) for c in spec.conditions)
        cold = spec.eligibility.cold_start if spec.eligibility is not None else None
        if cold is not None and any(col not in out.columns for col in start_context_columns(cold)):
            starts.append(StartContextSpec.from_requirement(cold))
    wanted = sorted({b for b in roc_bases if f"{b}{ROC_SUFFIX}" not in out.columns})
    if wanted:
        out = compute_rate_of_change(out, wanted)
    if starts:
        unique = {s.key: s for s in starts}
        out = append_start_context(out, list(unique.values()))
    return out


def recipe_input_inventory(frame: pd.DataFrame, recipe: Recipe) -> RecipeInventory:
    """Channel coverage and contract eligibility of ``recipe`` on ``frame``.

    ``frame`` should already carry :func:`with_contract_context`; a missing
    derivative is otherwise reported as the ``rate_unavailable`` refusal the
    daemon would give.
    """
    spec = recipe_serving_spec(recipe)
    channels = []
    for group in required_channel_groups(spec):
        recorded = any(
            name in frame.columns and np.isfinite(numeric_values(frame, name)).any()
            for name in group
        )
        present = np.zeros(len(frame), dtype=bool)
        fresh = np.zeros(len(frame), dtype=bool)
        for name in group:
            present |= np.isfinite(numeric_values(frame, name))
            fresh |= _fresh(frame, name)
        channels.append(
            ChannelCoverage(
                group=tuple(group),
                recorded=bool(recorded),
                present=int(present.sum()),
                fresh=int(fresh.sum()),
            )
        )
    reasons = eligibility_reasons(frame, spec, include_actual=True)
    eligible = np.asarray(pd.isna(reasons), dtype=bool)
    refusals = Counter(reason_code(str(r)) for r in reasons[~eligible])
    rpm = numeric_values(frame, RPM_COLUMN)
    with np.errstate(invalid="ignore"):
        running = int((rpm >= ENGINE_RUNNING_MIN_RPM).sum())
    return RecipeInventory(
        recipe=recipe.name,
        rows=len(frame),
        running_rows=running,
        channels=channels,
        eligible=int(eligible.sum()),
        refusals=dict(sorted(refusals.items(), key=lambda kv: (-kv[1], kv[0]))),
    )


def trim_bank_facts(frame: pd.DataFrame) -> dict[str, Any]:
    """Per-bank trim pairing, fuel status and thermal context on running rows.

    ``paired_<bank>`` counts rows where that bank's STFT *and* LTFT are both
    fresh (what a total-trim value needs); ``missing_bank`` counts rows where
    exactly one bank is paired — coverage for a bank difference that does not
    exist, reported instead of a zero difference. ``closed_loop`` uses the
    explicit membership :data:`CLOSED_LOOP_FUEL_STATES`.
    """
    rpm = numeric_values(frame, RPM_COLUMN)
    with np.errstate(invalid="ignore"):
        running = rpm >= ENGINE_RUNNING_MIN_RPM
    paired = {
        bank: _fresh(frame, stft) & _fresh(frame, ltft) & running
        for bank, (stft, ltft) in TRIM_BANKS.items()
    }
    both = paired["b1"] & paired["b2"]
    status = numeric_values(frame, FUEL_STATUS_COLUMN)
    status_fresh = _fresh(frame, FUEL_STATUS_COLUMN) & running
    states = Counter(
        fuel_status_name(float(code)) for code in status[status_fresh] if np.isfinite(code)
    )
    closed = status_fresh & np.isin(status, np.asarray(CLOSED_LOOP_FUEL_STATES, dtype=float))
    modes = (
        frame[MODE_COLUMN].astype(str).to_numpy()
        if MODE_COLUMN in frame.columns
        else np.full(len(frame), "unknown", dtype=object)
    )
    bank_diff = numeric_values(frame, "obd:SHORT_FUEL_TRIM_1") - numeric_values(
        frame, "obd:SHORT_FUEL_TRIM_2"
    )
    return {
        "running_rows": int(running.sum()),
        "paired_b1": int(paired["b1"].sum()),
        "paired_b2": int(paired["b2"].sum()),
        "paired_both": int(both.sum()),
        "missing_bank": int((paired["b1"] ^ paired["b2"]).sum()),
        "fuel_status_fresh": int(status_fresh.sum()),
        "fuel_status_states": dict(states.most_common()),
        "closed_loop_both_banks_paired": int((closed & both).sum()),
        "closed_loop_both_paired_warmup_mode": int((closed & both & (modes == "warmup")).sum()),
        "stft_bank_difference_exactly_zero": int((both & (bank_diff == 0.0)).sum()),
        "modes_running": dict(Counter(modes[running].tolist()).most_common()),
    }


#: J1979 trim PIDs decode as ``B * 100/128 - 100``: every real reading lands
#: on that lattice, and the ECM clamps short trims at +-25 %.
TRIM_LSB = 100.0 / 128.0


def trim_unit_checks(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Per trim channel: range, quantization and how much of it is exactly zero.

    Units and identity before fitting (B05.1). A channel whose values do not
    sit on the :data:`TRIM_LSB` lattice is not the PID it is labelled as; a
    channel that is *all* zero is a real state (this vehicle's long-term
    trims are zero after a flash until they relearn), not trim content, and a
    total-trim fit on such a session is an STFT fit under another name.
    """
    out: dict[str, dict[str, Any]] = {}
    for channels in TRIM_BANKS.values():
        for channel in channels:
            values = numeric_values(frame, channel)
            finite = values[np.isfinite(values)]
            if not finite.size:
                out[channel] = {"rows": 0, "recorded": False}
                continue
            steps = (finite + 100.0) / TRIM_LSB
            out[channel] = {
                "rows": int(finite.size),
                "recorded": True,
                "min": float(finite.min()),
                "max": float(finite.max()),
                "on_lsb_lattice": float(np.mean(np.abs(steps - np.round(steps)) < 1e-6)),
                "exactly_zero": float(np.mean(finite == 0.0)),
            }
    return out


def fuel_system_state_agreement(session: Path) -> dict[str, Any]:
    """PID 0x03 byte A (fuel system 1) vs byte B (fuel system 2), per sample.

    The collector stores the whole response payload but decodes only byte A
    into ``obd:FUEL_STATUS``; a bank-2 recipe gated on it assumes the two
    systems share one state. This counts, sample by sample, how often they do.
    Returns empty counts when the session stored no payloads.
    """
    import duckdb

    files = sorted(session.glob("telemetry-*.parquet"))
    if not files:
        return {"samples": 0}
    pattern = str(session / "telemetry-*.parquet")
    rows = duckdb.sql(
        "select hex(payload) as p, count(*) as n from read_parquet(?, union_by_name=true) "
        "where source='obd' and channel='FUEL_STATUS' group by 1",
        params=[pattern],
    ).fetchall()
    pairs: Counter[str] = Counter()
    samples = 0
    for payload, n in rows:
        samples += int(n)
        if payload is None or len(payload) < 4:
            pairs["no_payload"] += int(n)
            continue
        a, b = int(payload[0:2], 16), int(payload[2:4], 16)
        pairs[f"{fuel_status_name(float(a))}/{fuel_status_name(float(b))}"] += int(n)
    with_payload = samples - pairs.get("no_payload", 0)
    agree = sum(
        n
        for key, n in pairs.items()
        if key != "no_payload" and key.split("/")[0] == key.split("/")[1]
    )
    return {
        "samples": samples,
        "with_payload": with_payload,
        "systems_agree": agree,
        "pairs": dict(pairs.most_common()),
    }


def summarize(
    per_session: Sequence[Mapping[str, Any]], *, min_rows_per_drive: Mapping[str, int]
) -> dict[str, Any]:
    """Per recipe: eligible rows and qualifying independent drives.

    ``per_session`` entries carry ``drive_id`` and ``recipes`` (name →
    :meth:`RecipeInventory.as_dict`). A drive qualifies for a recipe when its
    pieces together contribute at least ``min_rows_per_drive[recipe]``
    eligible rows — the independent-evidence count B02 gates on.
    """
    totals: dict[str, dict[str, Any]] = {}
    for entry in per_session:
        drive = str(entry["drive_id"])
        for name, inv in entry["recipes"].items():
            slot = totals.setdefault(
                name,
                {"eligible": 0, "drives": Counter(), "absent_in_sessions": Counter()},
            )
            slot["eligible"] += int(inv["eligible"])
            slot["drives"][drive] += int(inv["eligible"])
            for channel in inv["absent_channels"]:
                slot["absent_in_sessions"][channel] += 1
    out: dict[str, Any] = {}
    for name, slot in totals.items():
        floor = min_rows_per_drive.get(name, 1)
        qualifying = sorted(d for d, n in slot["drives"].items() if n >= floor)
        out[name] = {
            "eligible": slot["eligible"],
            "sessions": len(per_session),
            "drives_with_eligible_rows": sum(1 for n in slot["drives"].values() if n > 0),
            "qualifying_drives": qualifying,
            "min_rows_per_drive": floor,
            "absent_in_sessions": dict(slot["absent_in_sessions"].most_common()),
        }
    return out


__all__ = [
    "TRIM_LSB",
    "ChannelCoverage",
    "RecipeInventory",
    "TRIM_BANKS",
    "fuel_system_state_agreement",
    "recipe_input_inventory",
    "summarize",
    "trim_bank_facts",
    "trim_unit_checks",
    "with_contract_context",
]

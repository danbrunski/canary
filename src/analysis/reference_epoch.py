"""Frozen reference epoch for fingerprint drift trending (Task 4.3,
eval-longitudinal-and-dtc.md).

A snapshot of what "normal" looked like across a hand-picked set of healthy
sessions' fingerprints (Task 4.1) — per-stat mean/std/quantiles. Retraining
never touches this file; only ``scripts/fingerprint.py freeze`` writes it, on
explicit operator command (a ledger ``corpus_boundary`` event is the prompt
to consciously re-freeze, not any automatic process — see
:mod:`src.analysis.vehicle_ledger`). Task 4.4's trend tests compare each new
session's fingerprint against this frozen band, not against a rolling
baseline that would quietly absorb slow drift.

Storage: a single JSON file (not append-only like the ledgers — there is
exactly one *current* reference epoch at a time; re-freezing overwrites it).
Profile-scoped via ``src.profiles.scope_config`` like the sibling
``daemon.*`` paths.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from src.storage.sidecar import read_json_object, write_json_sidecar
from src.timefmt import format_timestamp as _format_timestamp

DEFAULT_REFERENCE_EPOCH_PATH = Path("data/control/reference_epoch.json")

SCHEMA_VERSION = 1

#: Quantiles recorded per stat, as fractions. Rendered as ``p<N>`` keys.
QUANTILES: tuple[float, ...] = (0.1, 0.5, 0.9)


@dataclass(frozen=True)
class StatSummary:
    """Distribution summary for one fingerprint stat across the epoch's sessions."""

    mean: float
    std: float
    quantiles: dict[str, float]
    n: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "mean": self.mean,
            "std": self.std,
            "quantiles": dict(self.quantiles),
            "n": self.n,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StatSummary:
        return cls(
            mean=float(data["mean"]),
            std=float(data["std"]),
            quantiles={str(k): float(v) for k, v in dict(data.get("quantiles") or {}).items()},
            n=int(data.get("n", 0)),
        )


@dataclass(frozen=True)
class ReferenceEpoch:
    """Full frozen-epoch payload."""

    frozen_at: str
    sessions: tuple[str, ...]
    stats: dict[str, StatSummary]
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "frozen_at": self.frozen_at,
            "sessions": list(self.sessions),
            "stats": {name: summary.to_dict() for name, summary in self.stats.items()},
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReferenceEpoch:
        return cls(
            frozen_at=str(data["frozen_at"]),
            sessions=tuple(str(s) for s in data.get("sessions", [])),
            stats={
                str(k): StatSummary.from_dict(v) for k, v in dict(data.get("stats") or {}).items()
            },
            notes=str(data.get("notes", "")),
        )


def build_reference_epoch(
    fingerprints: Mapping[str, Mapping[str, float]],
    *,
    notes: str = "",
    now: datetime | None = None,
) -> ReferenceEpoch:
    """Aggregate ``{session_id: {stat_name: value}}`` into a frozen epoch.

    Each stat's mean/std/quantiles are computed over exactly the sessions
    that carried that stat — a session whose fingerprint omitted a stat
    (precondition absent, Task 4.1) doesn't pull that stat's distribution
    toward zero.
    """
    per_stat: dict[str, list[float]] = {}
    for stats in fingerprints.values():
        for name, value in stats.items():
            per_stat.setdefault(name, []).append(float(value))

    stat_summaries: dict[str, StatSummary] = {}
    for name, values in per_stat.items():
        arr = np.asarray(values, dtype=float)
        quantiles = {f"p{int(round(q * 100))}": float(np.quantile(arr, q)) for q in QUANTILES}
        stat_summaries[name] = StatSummary(
            mean=float(arr.mean()),
            std=float(arr.std()) if arr.size > 1 else 0.0,
            quantiles=quantiles,
            n=int(arr.size),
        )

    frozen_at = _format_timestamp(datetime.now(UTC) if now is None else now)
    return ReferenceEpoch(
        frozen_at=frozen_at,
        sessions=tuple(sorted(fingerprints.keys())),
        stats=stat_summaries,
        notes=notes,
    )


def write_reference_epoch(path: Path, epoch: ReferenceEpoch) -> Path:
    """Overwrite the reference-epoch file. The only writer of this path — see
    module docstring; nothing else in the codebase calls this."""
    write_json_sidecar(path, epoch.to_dict(), mkdir=True)
    return path


def read_reference_epoch(path: Path) -> ReferenceEpoch | None:
    """Load the frozen epoch, or ``None`` if absent / unreadable."""
    data = read_json_object(path)
    if data is None:
        return None
    try:
        return ReferenceEpoch.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_REFERENCE_EPOCH_PATH",
    "QUANTILES",
    "SCHEMA_VERSION",
    "ReferenceEpoch",
    "StatSummary",
    "build_reference_epoch",
    "read_reference_epoch",
    "write_reference_epoch",
]

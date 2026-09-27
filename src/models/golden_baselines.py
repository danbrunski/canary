"""Golden-anchored disposition baseline sidecar (Task 3.7,
online-training-strategy.md).

:class:`GoldenBaselines` (median/MAD + Task 1.6 value sets per
``(channel, operating_mode)``, computed by
``src.analysis.training_disposition.compute_golden_baselines``) is frozen
once per golden declaration and needs to persist across process runs — the
same "declare golden" CLI action today (``scripts/ledger.py golden``) and the
promotion gate's future callers both need to read it back. This repo's
convention for a larger structured artifact is a JSON sidecar living next to
the thing it describes (mirrors ``fingerprint.json``/``classification.json``
next to a session, or ``model.manifest.json`` next to a model artifact)
rather than inflating the already-JSONL ``TrainingRunRecord`` with a second,
differently-shaped payload. The training ledger is the natural "next to"
here — it's already profile-scoped (``src.vehicle.profiles.scope_config``'s
``training_ledger`` field), so a sibling directory inherits that scoping for
free without a new config field.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "ChannelBaseline",
    "GoldenBaselines",
    "golden_baselines_path",
    "read_golden_baselines",
    "write_golden_baselines",
]


@dataclass(frozen=True)
class ChannelBaseline:
    """One frozen ``(channel, operating_mode)`` baseline (Task 3.7) — the
    statistics a golden run's input sessions produced, captured at
    declaration time so a later candidate window is screened against a fixed
    reference instead of its own (self-referential) statistics.

    Exactly one of ``(median, scaled_mad)`` / ``baseline_values`` is
    populated, matching the continuous/discrete split of
    :class:`~src.analysis.training_disposition.ChannelDisposition` —
    ``is_discrete`` says which.
    """

    channel: str
    operating_mode: str | None
    is_discrete: bool
    median: float | None = None
    scaled_mad: float | None = None
    baseline_values: tuple[float, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "operating_mode": self.operating_mode,
            "is_discrete": self.is_discrete,
            "median": self.median,
            "scaled_mad": self.scaled_mad,
            "baseline_values": (
                list(self.baseline_values) if self.baseline_values is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ChannelBaseline:
        raw_values = data.get("baseline_values")
        return cls(
            channel=str(data["channel"]),
            operating_mode=(
                None if data.get("operating_mode") is None else str(data["operating_mode"])
            ),
            is_discrete=bool(data.get("is_discrete", False)),
            median=(None if data.get("median") is None else float(data["median"])),
            scaled_mad=(None if data.get("scaled_mad") is None else float(data["scaled_mad"])),
            baseline_values=(None if raw_values is None else tuple(raw_values)),
        )


@dataclass(frozen=True)
class GoldenBaselines:
    """Frozen per-``(channel, operating_mode)`` baselines for one golden run
    (Task 3.7). JSON-safe; persisted by ``src.models.golden_baselines``
    alongside the training ledger, re-frozen every time the operator
    re-declares golden for that ``model_type``."""

    baselines: tuple[ChannelBaseline, ...] = field(default_factory=tuple)

    def get(self, channel: str, operating_mode: str | None) -> ChannelBaseline | None:
        """The frozen baseline for this exact ``(channel, operating_mode)``,
        or ``None`` when golden's own input sessions never covered it (the
        caller's documented fallback: screen that group window-local)."""
        return next(
            (
                b
                for b in self.baselines
                if b.channel == channel and b.operating_mode == operating_mode
            ),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"baselines": [b.to_dict() for b in self.baselines]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GoldenBaselines:
        return cls(baselines=tuple(ChannelBaseline.from_dict(b) for b in data.get("baselines", [])))


def golden_baselines_path(ledger_path: Path, model_type: str) -> Path:
    """Sidecar path for ``model_type``'s frozen golden baselines: a
    ``golden_baselines/`` directory next to the (profile-scoped) training
    ledger file, one JSON per model type (a profile can golden-declare more
    than one detector independently)."""
    return Path(ledger_path).parent / "golden_baselines" / f"{model_type}.json"


def write_golden_baselines(path: Path, baselines: GoldenBaselines) -> None:
    """Serialize ``baselines`` to ``path``, creating parent dirs as needed.
    Re-declaring golden calls this again — the doc's "re-declaring golden
    re-freezes the baselines" — so this always overwrites, never merges."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baselines.to_dict(), indent=2, sort_keys=True) + "\n")


def read_golden_baselines(path: Path) -> GoldenBaselines | None:
    """Load the frozen baselines at ``path``, or ``None`` when absent/
    unreadable — callers treat that exactly like "no golden run declared":
    fall back to window-local screening (see ``classify_dispositions``)."""
    if not path.exists():
        return None
    try:
        return GoldenBaselines.from_dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        log.warning("golden baselines unreadable at %s: %s", path, exc)
        return None

"""Golden-anchored disposition baseline sidecar (Task 3.7,
online-training-strategy.md).

``training_disposition.GoldenBaselines`` (median/MAD + Task 1.6 value sets
per ``(channel, operating_mode)``) is frozen once per golden declaration and
needs to persist across process runs — the same "declare golden" CLI action
today (``scripts/ledger.py golden``) and the promotion gate's future callers
both need to read it back. This repo's convention for a larger structured
artifact is a JSON sidecar living next to the thing it describes (mirrors
``fingerprint.json``/``classification.json`` next to a session, or
``model.manifest.json`` next to a model artifact) rather than inflating the
already-JSONL ``TrainingRunRecord`` with a second, differently-shaped
payload. The training ledger is the natural "next to" here — it's already
profile-scoped (``src.profiles.scope_config``'s ``training_ledger`` field),
so a sibling directory inherits that scoping for free without a new config
field.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.analysis.training_disposition import GoldenBaselines

log = logging.getLogger(__name__)

__all__ = ["golden_baselines_path", "read_golden_baselines", "write_golden_baselines"]


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

"""Re-derive a prediction's noise floor from the file it cites (E01.4).

A :class:`~src.evidence.records.NoiseFloorRef` is a copy of one channel of a
measured ``noise_floor.yaml``. A copy can be edited: the WOT floor relabelled
"idle", or an sd invented outright, would pass every check made on the copy
alone. So the ledger re-reads the cited file at append time and refuses the
reference unless the file's bytes hash to ``source_sha256`` and its entry for
``quantity`` states exactly this sd, unit and regime. A floor is only ever
what was measured.

``source`` is the file's path, relative to the repository root when it lies
inside the checkout (``data/<profile>/noise_floor.yaml`` is tracked, so the
same reference resolves on the laptop and the Pi), absolute otherwise.
Stdlib + PyYAML; the schema read here is ``src.analysis.noise_floor``'s
``channels: {<quantity>: {unit, conditions, sd}}``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from src.evidence.records import DETECTABLE_SIGMA, NoiseFloorRef
from src.paths import REPO_ROOT

__all__ = ["FloorVerificationError", "floor_source_path", "floor_source_ref", "verify_floor"]


class FloorVerificationError(ValueError):
    """The floor a prediction states is not the floor its source measured."""


def floor_source_ref(path: Path) -> str:
    """How a floor file is named in a reference: repo-relative when possible."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def floor_source_path(source: str) -> Path:
    path = Path(source)
    return path if path.is_absolute() else REPO_ROOT / path


def verify_floor(ref: NoiseFloorRef) -> None:
    """Refuse ``ref`` unless its source file states exactly this floor."""
    path = floor_source_path(ref.source)
    if not path.is_file():
        raise FloorVerificationError(
            f"the noise floor for {ref.quantity} cites {ref.source!r}, which does not exist; "
            "a floor is only what a null run measured"
        )
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref.source_sha256:
        raise FloorVerificationError(
            f"{ref.source!r} no longer hashes to the cited source_sha256: derive the floor "
            "again from the file as it stands (src.analysis.noise_floor.floor_reference)"
        )
    data: Any = yaml.safe_load(raw) or {}
    entry = (data.get("channels") or {}).get(ref.quantity) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        raise FloorVerificationError(f"{ref.source!r} measured no floor for {ref.quantity!r}")
    differ = [
        name
        for name, stated, measured in (
            ("sd", ref.sd.value, entry.get("sd")),
            ("unit", ref.sd.unit, entry.get("unit")),
            ("conditions", ref.conditions, entry.get("conditions")),
        )
        if stated != measured
    ]
    if differ:
        raise FloorVerificationError(
            f"the floor stated for {ref.quantity} differs from {ref.source!r} in "
            f"{', '.join(differ)} (measured under {entry.get('conditions')!r}); "
            "a floor cannot be relabelled or edited"
        )
    if ref.sigma_threshold < DETECTABLE_SIGMA:
        raise FloorVerificationError(
            f"sigma_threshold {ref.sigma_threshold:g} is below {DETECTABLE_SIGMA:g}: "
            "a lower bar would call noise an effect"
        )

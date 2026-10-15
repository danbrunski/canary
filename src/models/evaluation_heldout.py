"""Which recordings an artifact has seen, and what that means per detector (A02.3).

A drive is held out only if no compared detector trained, calibrated, or chose
its alert level on any of its sessions. Training and calibration make the
whole drive unusable (the baselines fit and threshold on those sessions too);
a budget level makes only that artifact's detectors in-sample there (operator
decision 2026-10-05, option b). Extracted from ``scripts/evaluate_detectors.py``
(R15.4); that script re-exports these names.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.models.budget_level import budget_level_sessions
from src.preprocessing.drives import parent_session


def record_seen(seen: dict[str, dict[str, str]], label: str, manifest: Any) -> None:
    """Note every recording ``manifest`` trained or calibrated on, or chose
    its alert level on (A02.3), by directory.

    Manifests name the pieces they used, and a recording split at a data gap
    is named ``<dir>#segment-00N``; drives list directories. Recording the
    parent directory is what lets a drive the detector saw part of be
    skipped (the 2026-09-23 candidates name ``2026-09-21T143331#segment-001``).
    """
    for role in ("training_sessions", "calibration_sessions"):
        for name in getattr(manifest, role, ()) or ():
            seen.setdefault(parent_session(str(name)), {})[label] = role.removesuffix("_sessions")
    # A02.3 (operator decision 2026-10-05, option b): the drives a sequence
    # artifact's alert level was chosen on are seen too. A false-alarm rate
    # there is in-sample to the level, so neither the A06.5 gate nor the
    # benchmark may count it as held out.
    for name in budget_level_sessions(manifest):
        seen.setdefault(parent_session(name), {})[label] = "budget_level"


#: A02.3: the harness detectors whose verdicts rest on each artifact. A drive
#: one artifact chose its alert level on is in-sample for these, and only these.
DETECTORS_OF_ARTIFACT: dict[str, tuple[str, ...]] = {
    "isolation_forest": ("isolation_forest", "fused"),
    "sequence": ("sequence", "sequence_global", "fused"),
}


#: The seen roles that keep a drive in the evaluation for the other detectors.
PER_DETECTOR_SEEN_ROLES = frozenset({"budget_level"})


def in_sample_detectors(seen_by: Mapping[str, Mapping[str, str]]) -> dict[str, str] | None:
    """For a drive whose sessions some artifact saw (``seen_by``: session ->
    {artifact label: role}), the detectors in-sample there and why, or
    ``None`` when the drive must be skipped for every detector.

    Training and calibration skip the whole drive (the baselines fit and
    threshold on those sessions too). A budget level (A02.3, operator decision
    2026-10-05) makes only that artifact's detectors in-sample: the IF and the
    baselines are still scored on the drive.
    """
    out: dict[str, str] = {}
    for session, roles in sorted(seen_by.items()):
        for label, role in roles.items():
            if role not in PER_DETECTOR_SEEN_ROLES:
                return None
            for detector in DETECTORS_OF_ARTIFACT.get(label, (label,)):
                out.setdefault(detector, f"{label} {role} on {session}")
    return out

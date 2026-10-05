"""Which recordings represent the default profile (operator decision 2026-09-24).

Torch-free and pandas-free on purpose: the brain's analysis child
(``src.control.brain_jobs``) reads sessions through E03's change evaluation,
and importing this rule from ``src.models.evaluation`` pulled the detector
stack (and torch) into a child capped at the digest's address space.
"""

from __future__ import annotations

from pathlib import Path

from src.session import SessionKind, session_kind
from src.storage.capture_manifest import read_capture_manifest

__all__ = ["default_profile_exclusion"]


def default_profile_exclusion(session_dir: Path) -> str | None:
    """Why a session's rows are kept out of detector evaluation, or ``None``.

    A ``_tuning-*`` session, or a capture whose manifest says it is not
    corpus-eligible, is excluded: it still bridges its drive (drive grouping
    keeps a drive's default parts together, so held-out stays honest), but its
    rows reach no scored row, fault injection or metric. A capture without a
    readable manifest is treated as the replay guard treats it (R03.12):
    eligible.
    """
    name = Path(session_dir).name
    # Operator decision 2026-09-24: tuning logs do not represent the default
    # profile, so the detectors are neither trained, calibrated, scored live
    # (R03.12) nor evaluated on them. The name rule is src.session's.
    if session_kind(name) is SessionKind.TUNING:
        return "tuning session: not the default profile (operator decision 2026-09-24)"
    manifest = read_capture_manifest(Path(session_dir))
    if manifest is not None and not manifest.corpus_eligible:
        return (
            f"capture profile {manifest.profile!r} is not corpus-eligible "
            "(operator decision 2026-09-24)"
        )
    return None

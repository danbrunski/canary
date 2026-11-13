"""R26.7 / R26.8 — ``digest`` and ``channels`` were split into admission, plan
and alignment modules and re-export every name callers use, so each one is
the object its owner defines, not a copy.

A copy would pass a name check and still break a test that patches the owner's
global: the moved function reads *its* module's global, not the facade's.
"""

from __future__ import annotations

import ast
import logging

import pytest

import src.analysis.channels as channels
import src.analysis.channels_align as channels_align
import src.analysis.channels_plan as channels_plan
import src.analysis.digest as digest
import src.analysis.digest_admission as digest_admission
from src.paths import REPO_ROOT

_ANALYSIS = REPO_ROOT / "src" / "analysis"


@pytest.mark.parametrize("name", channels.__all__)
def test_channels_reexports_are_the_owners_objects(name: str) -> None:
    owners = [m for m in (channels_plan, channels_align) if hasattr(m, name)]
    assert owners, name
    assert all(getattr(channels, name) is getattr(m, name) for m in owners)


@pytest.mark.parametrize("name", [n for n in digest.__all__ if hasattr(digest_admission, n)])
def test_digest_reexports_are_the_owners_objects(name: str) -> None:
    assert getattr(digest, name) is getattr(digest_admission, name)


def test_the_refusal_warning_is_still_logged_under_the_digest_logger() -> None:
    """The Pi's logs and ``caplog`` filters address ``src.analysis.digest``."""
    assert digest.log.name == "src.analysis.digest"
    assert digest_admission.log is logging.getLogger("src.analysis.digest")


def test_one_lock_guards_the_top_level_answer() -> None:
    """``write_digest`` (here) and ``write_digest_skipped`` (admission) must
    take the same lock; a second copy would let a refusal and a digest
    interleave."""
    assert vars(digest)["_TOP_LEVEL_LOCK"] is digest_admission._TOP_LEVEL_LOCK


def _reads_session(module: str) -> bool:
    tree = ast.parse((_ANALYSIS / f"{module}.py").read_text())
    return any(isinstance(n, ast.Name) and n.id == "read_session" for n in ast.walk(tree))


def test_every_session_read_of_channels_goes_through_one_module() -> None:
    """Tests count scans by patching ``channels_plan.read_session``; a read
    left in another module would escape the count."""
    assert _reads_session("channels_plan")
    assert not _reads_session("channels")
    assert not _reads_session("channels_align")

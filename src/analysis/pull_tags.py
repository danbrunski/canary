"""The one vocabulary for tags that mark a pull (H07.2).

A pull is tagged by what the operator physically did (AGENTS.md: tag physical
actions, not graph selections): the start and the end of a wide-open-throttle
run. Two producers wrote two spellings — the Logging page ``pull_start`` /
``pull_end``, the guided sessions and the capture docs ``tuning_pull_start``
/ ``tuning_pull_end`` — and the digest counted only the second, so every pull
tagged from the page read as untagged. Producers write :data:`PULL_START` and
:data:`PULL_END`; readers use :func:`is_pull_start` / :func:`is_pull_end`,
which still accept the older spellings so sessions already tagged keep
counting.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

PULL_START: Final = "tuning_pull_start"
PULL_END: Final = "tuning_pull_end"

#: Every label that has ever marked a pull start: the canonical one, the
#: guided sessions' single-instant ``wot_pull``, and the Logging page's old
#: ``pull_start``.
PULL_START_LABELS: Final = frozenset({PULL_START, "wot_pull", "pull_start"})
PULL_END_LABELS: Final = frozenset({PULL_END, "pull_end"})


def is_pull_start(tag: Mapping[str, object]) -> bool:
    return tag.get("label") in PULL_START_LABELS


def is_pull_end(tag: Mapping[str, object]) -> bool:
    return tag.get("label") in PULL_END_LABELS


__all__ = [
    "PULL_END",
    "PULL_END_LABELS",
    "PULL_START",
    "PULL_START_LABELS",
    "is_pull_end",
    "is_pull_start",
]

"""One pull-tag vocabulary (H07.2): what the Logging page writes is what the
digest counts, enforced rather than remembered."""

from __future__ import annotations

import re
from pathlib import Path

from src.analysis.pull_tags import (
    PULL_END,
    PULL_END_LABELS,
    PULL_START,
    PULL_START_LABELS,
    is_pull_end,
    is_pull_start,
)

LOGGING_PAGE = Path("src/dashboard/static/src/panels/tuning/LoggingPage.tsx")


def _page_labels() -> list[str]:
    block = (
        LOGGING_PAGE.read_text().split("const PHYSICAL_TAGS = [", 1)[1].split("] as const", 1)[0]
    )
    return re.findall(r'\["([a-z_]+)",', block)


def test_the_logging_page_writes_the_canonical_pull_labels() -> None:
    labels = _page_labels()
    assert PULL_START in labels and PULL_END in labels
    pull_labels = [label for label in labels if "pull" in label]
    assert set(pull_labels) == {PULL_START, PULL_END}


def test_every_spelling_that_ever_marked_a_pull_still_counts() -> None:
    for label in ("tuning_pull_start", "wot_pull", "pull_start"):
        assert is_pull_start({"label": label})
    for label in ("tuning_pull_end", "pull_end"):
        assert is_pull_end({"label": label})
    assert not is_pull_start({"label": "steady_hold_start"})
    assert PULL_START in PULL_START_LABELS and PULL_END in PULL_END_LABELS

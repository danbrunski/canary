"""No test fixture dates an override's review_by within reach of the clock.

An accepted-baseline override is active until ``provenance.review_by``, judged
against ``date.today()`` unless the caller passes ``today=``. Five fixtures
meant "an active override" and wrote ``review_by`` as 2026-11-17, six months
after they were written; on 2026-11-19 seven tests went red at once and the
push gate refused an unrelated dashboard commit. A fixture is either expired
(a year before the project began), active forever (2099-01-01), or pins the
clock with ``today=`` -- never a near-future date that silently changes
meaning on some later morning.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.paths import REPO_ROOT

TESTS = REPO_ROOT / "tests"

# review_by=date(2026, 11, 17) | "review_by": date(...) | review_by: '2026-11-17'
# | review_by="2026-11-17" | "review_by": "2026-11-17"
_DATE_CALL = re.compile(r"review_by\"?\s*[:=]\s*date\(\s*(\d{4})\s*,")
_ISO = re.compile(r"review_by\"?\s*[:=]\s*[\"']?(\d{4})-\d{2}-\d{2}")

#: Years a fixture may name without pinning the clock: long expired (the
#: project began in 2026, so 2025 and earlier are past for every run) or
#: never reached.
_EXPIRED_BEFORE = 2026
_ACTIVE_FROM = 2090


def _bombs(text: str) -> list[tuple[int, str]]:
    """``(line, text)`` of each review_by the clock will reach, unless the file
    pins the clock with ``today=`` where it judges expiry."""
    if "today=" in text:
        return []
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in (*_DATE_CALL.finditer(line), *_ISO.finditer(line)):
            if _EXPIRED_BEFORE <= int(match.group(1)) < _ACTIVE_FROM:
                hits.append((lineno, line.strip()))
    return hits


def test_no_fixture_review_by_is_a_date_the_clock_will_reach() -> None:
    files = [
        p
        for p in sorted((*TESTS.rglob("*.py"), *TESTS.rglob("*.yaml")))
        if p.name != Path(__file__).name
    ]
    hits = [
        f"{path.relative_to(REPO_ROOT)}:{lineno}: {line}"
        for path in files
        for lineno, line in _bombs(path.read_text(encoding="utf-8"))
    ]
    assert not hits, (
        "a review_by the clock will reach turns an active override into an expired one "
        "on some later day; use date(2099, 1, 1) for active, a pre-2026 date for expired, "
        "or pass today= where expiry is judged:\n" + "\n".join(hits)
    )


def test_the_scan_sees_each_spelling() -> None:
    for spelling in (
        "review_by=date(2026, 11, 17)",
        '"review_by": date(2030, 1, 1)',
        "review_by: '2026-11-17'",
        'review_by="2027-01-01"',
        '"review_by": "2026-11-17"',
    ):
        assert _bombs(spelling), spelling
    assert _bombs("review_by=date(2099, 1, 1)\nreview_by: '2020-01-01'\n") == []
    assert _bombs("review_by=date(2026, 11, 17)\nx.is_expired(today=date(2026, 12, 1))\n") == []

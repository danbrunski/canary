"""R27.1: one rule for an operator override's reason, and no sixth copy of it."""

from __future__ import annotations

import argparse
import ast
import re

import pytest

from src.models.override_reason import (
    BLANK_OVERRIDE_REASON,
    require_override_reason,
    stated_override_reason,
)
from src.paths import REPO_ROOT


@pytest.mark.parametrize("blank", ["", " ", "\t\n  "])
def test_a_blank_reason_is_refused(blank: str) -> None:
    with pytest.raises(ValueError, match=BLANK_OVERRIDE_REASON):
        require_override_reason(blank)
    with pytest.raises(ValueError, match="^gate wording$"):
        require_override_reason(blank, refusal="gate wording")
    assert stated_override_reason(blank) is None


def test_a_reason_is_returned_stripped() -> None:
    assert require_override_reason("  bench drive\n") == "bench drive"
    assert stated_override_reason("  bench drive\n") == "bench drive"
    assert stated_override_reason(None) is None


def test_the_cli_type_reports_the_shared_refusal_as_an_argparse_error() -> None:
    from scripts._cli import override_reason

    with pytest.raises(argparse.ArgumentTypeError, match=f"^{BLANK_OVERRIDE_REASON}$"):
        override_reason("  ")
    assert override_reason(" bench ") == "bench"


#: Hand-written forms of the rule, written broadly enough to catch a new copy
#: under the repo's real override names (``*override*``, ``accept_*``) or a
#: ``reason`` variable. ``test_the_guard_matches_each_retired_form`` pins the
#: six pre-R27.1 copies, each of which one of these matches;
#: ``if not calibration_override:`` is the one that let ``"  "`` through.
_HAND_WRITTEN_RULE = re.compile(
    # an override or ``accept_*`` value stripped on the same line
    r"(?:override|\baccept_\w+).*\.strip\(\)"
    # a ``reason`` stripped (``reason = value.strip()``, ``not reason.strip()``)
    r"|\breason\b.*\.strip\(\)"
    # a blank test by truthiness
    r"|\bif not (?:\w*override(?:_reason)?|accept_\w+|reason)\s*:"
    # the job queue's shape
    r"|isinstance\(\w*reason\w*,\s*str\)\s*or\s+not\b"
)

#: Matches that are not an operator override's reason, by file and enclosing
#: function. Shrink-only: an entry that no longer matches fails the guard, so
#: delete it rather than let it shelter a future copy.
_NOT_AN_OVERRIDE: dict[tuple[str, str], str] = {
    (
        "src/analysis/rom_editor.py",
        "TypicalLeftEmpty._reason_is_text",
    ): "why a fillable ROM field has no typical value; a declaration, not an override",
    (
        "src/analysis/rom_levers.py",
        "_plant_text",
    ): "formats a plant qualification's reason for display; not an override",
    (
        "src/evidence/records.py",
        "WhatIfPoint._both_or_reason",
    ): "why a what-if point cannot be evaluated; a record field, not an override",
}

_SCANNED_ROOTS = ("src", "scripts", "tools")
_OWN_MODULE = "src/models/override_reason.py"


def _enclosing_function(tree: ast.Module, line: int) -> str:
    """Qualified name of the innermost function or class around ``line``."""
    best: tuple[int, str] = (-1, "<module>")

    def visit(node: ast.AST, prefix: str) -> None:
        nonlocal best
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}{child.name}"
                end = child.end_lineno or child.lineno
                if child.lineno <= line <= end and child.lineno > best[0]:
                    best = (child.lineno, name)
                visit(child, f"{name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return best[1]


def _hand_written_rule_sites() -> dict[tuple[str, str], list[str]]:
    sites: dict[tuple[str, str], list[str]] = {}
    for root in _SCANNED_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative == _OWN_MODULE:
                continue
            text = path.read_text(encoding="utf-8")
            hits = [
                (number, line.strip())
                for number, line in enumerate(text.splitlines(), 1)
                if _HAND_WRITTEN_RULE.search(line)
            ]
            if not hits:
                continue
            tree = ast.parse(text)
            for number, line in hits:
                key = (relative, _enclosing_function(tree, number))
                sites.setdefault(key, []).append(f"{relative}:{number}: {line}")
    return sites


def test_no_override_reason_check_is_written_outside_the_shared_rule() -> None:
    """Regrowth guard: a new gate calls ``require_override_reason`` (or
    ``stated_override_reason``) instead of re-deriving blank-text handling."""
    sites = _hand_written_rule_sites()
    offenders = [hit for key, hits in sites.items() if key not in _NOT_AN_OVERRIDE for hit in hits]
    assert offenders == []
    stale = sorted(key for key in _NOT_AN_OVERRIDE if key not in sites)
    assert stale == [], "allowlist entries no longer match; delete them"


def test_the_guard_matches_each_retired_form() -> None:
    for retired in (
        # isolation_forest._plan_mode
        "if not calibration_override:",
        # sequence_autoencoder_training
        "if calibration_override is None or not calibration_override.strip():",
        "override_record = {'reason': calibration_override.strip(), 'violations': violations}",
        # alert_budget.check_alert_budget
        "if override_reason is not None and not override_reason.strip():",
        # budget_level.unbudgeted_record
        "if not override_reason.strip():",
        # scripts/_cli.override_reason
        "reason = value.strip()",
        "if not reason:",
        # control/jobs.JobController.submit and _build_command
        "if not isinstance(reason, str) or not reason.strip():",
        "thin_reason = str(params.get('accept_thin_calibration') or '').strip()",
        # new copies under the repo's real override names
        "if not args.accept_alert_budget.strip():",
        "if not reason.strip():",
    ):
        assert _HAND_WRITTEN_RULE.search(retired), retired
    for unrelated in (
        "if not overrides:",
        "residual.unqualified_reason.strip()",
        "name = value.strip()",
    ):
        assert not _HAND_WRITTEN_RULE.search(unrelated), unrelated

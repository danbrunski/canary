#!/usr/bin/env bash
# PreToolUse guard for unattended /goal runs.
#
# Inert unless .claude/overnight-mode exists (created by /overnight,
# removed manually in the morning). While active, denies actions that
# should never happen without an operator watching: anything touching
# the Pi (ssh/scp/rsync/systemctl/sudo), pushes, and destructive rm/git
# that could wipe a concurrent session's uncommitted work.
#
# Contract: stdin = hook JSON; exit 2 = deny (reason on stderr, fed back
# to Claude); anything else = allow.
#
# FAIL-OPEN by design. The guard is a convenience that stops unattended
# mistakes, not a security boundary. If the guard itself malfunctions
# (python missing, a crash, a syntax slip in the body) it must NOT wedge
# the whole run by blocking every command. So only a *deliberate* deny
# (exit 2) blocks; any other exit status from the body is treated as a
# malfunction and allowed. Two structural safeguards make this real:
#   1. The Python logic lives in a quoted heredoc (<<'PY'), so it can
#      contain apostrophes/quotes freely -- the class of bug that once
#      made an inline `python3 -c '...'` unparseable is gone.
#   2. The body wraps its logic in try/except and fails open on any
#      unexpected exception, while still letting deny() propagate.
# The one residual failure mode we cannot self-heal is a syntax error in
# THIS bash wrapper (bash aborts with status 2, indistinguishable from a
# deny) -- so keep the wrapper trivial and put real logic in the heredoc.

set -u
FLAG="${CLAUDE_PROJECT_DIR:-.}/.claude/overnight-mode"
[ -f "$FLAG" ] || exit 0

INPUT=$(cat)

GUARD_INPUT="$INPUT" python3 <<'PY'
import json
import os
import re
import sys


def deny(reason):
    print("overnight guard: " + reason, file=sys.stderr)
    sys.exit(2)


def evaluate():
    raw = os.environ.get("GUARD_INPUT", "") or ""
    try:
        payload = json.loads(raw)
    except Exception:
        return  # unparseable input: do not block

    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    if tool == "Bash":
        cmd = tool_input.get("command", "") or ""
        rules = [
            (r"\b(ssh|scp|rsync)\b", "no Pi/remote access during unattended runs"),
            (r"\bsudo\b", "no privileged commands during unattended runs"),
            (r"\b(systemctl|shutdown|reboot|poweroff)\b", "no service/system control during unattended runs"),
            (r"\bgit\s+push\b", "no pushes during unattended runs; commits stay local for morning review"),
            # Tree-wide git commands can wipe a concurrent session's
            # uncommitted work. Block the destructive ones outright; the
            # implementer is told to clean up per-file, never tree-wide.
            (r"\bgit\s+reset\s+(--hard|--merge|--keep)\b", "no hard/merge git reset during unattended runs; it can wipe another session's uncommitted work"),
            (r"\bgit\s+clean\b[^|;&]*-\w*[fdx]", "no git clean during unattended runs; it can delete another session's untracked work"),
            (r"\bgit\s+(checkout|restore)\s+[.]", "no tree-wide git checkout/restore during unattended runs; revert per-file instead"),
            (r"\bgit\s+stash\b", "no git stash during unattended runs; it can pocket another session's uncommitted work"),
        ]
        for pattern, reason in rules:
            if re.search(pattern, cmd):
                deny(reason)
        # Destructive removal = RECURSION (-r/-R/--recursive). Plain
        # `rm -f file` has a bounded blast radius and is allowed; only
        # recursive deletes outside /tmp are denied.
        if re.search(r"\brm\s+([^|;&]*\s)?(-\w*[rR]\w*|--recursive)\b", cmd) and "/tmp/" not in cmd:
            deny("recursive rm outside /tmp is blocked during unattended runs")


try:
    evaluate()
except Exception:
    # Guard malfunction: fail open rather than wedge the run. A deliberate
    # deny() raises SystemExit (not Exception), so it is NOT caught here.
    sys.exit(0)
sys.exit(0)
PY
status=$?

# Only a deliberate deny (exit 2) blocks. Any other nonzero status means
# the body could not run (e.g. python3 missing) -- fail open.
[ "$status" -eq 2 ] && exit 2
exit 0

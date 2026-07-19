#!/usr/bin/env bash
# PreToolUse guard for unattended /goal runs.
#
# Inert unless .claude/overnight-mode exists (created by /overnight,
# removed manually in the morning). While active, denies actions that
# should never happen without an operator watching: anything touching
# the Pi (ssh/scp/rsync/systemctl/sudo), pushes, destructive rm, and
# the roadmap doc owned by another session.
#
# Contract: stdin = hook JSON; exit 0 = allow, exit 2 = deny with the
# reason on stderr (fed back to Claude).

set -u
FLAG="${CLAUDE_PROJECT_DIR:-.}/.claude/overnight-mode"
[ -f "$FLAG" ] || exit 0

INPUT=$(cat)

printf '%s' "$INPUT" | python3 -c '
import json
import re
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)  # unparseable input: do not block

tool = payload.get("tool_name", "")
tool_input = payload.get("tool_input", {}) or {}


def deny(reason):
    print("overnight guard: " + reason, file=sys.stderr)
    sys.exit(2)


if tool == "Bash":
    cmd = tool_input.get("command", "") or ""
    rules = [
        (r"\b(ssh|scp|rsync)\b", "no Pi/remote access during unattended runs"),
        (r"\bsudo\b", "no privileged commands during unattended runs"),
        (r"\b(systemctl|shutdown|reboot|poweroff)\b", "no service/system control during unattended runs"),
        (r"\bgit\s+push\b", "no pushes during unattended runs; commits stay local for morning review"),
        (r"mcp-and-power-hardening", "that roadmap doc is owned by another session"),
        # Tree-wide git commands can wipe a CONCURRENT session's uncommitted
        # work. Block the destructive ones outright; the implementer is told
        # to clean up per-file, never tree-wide.
        (r"\bgit\s+reset\s+(--hard|--merge|--keep)\b", "no hard/merge git reset during unattended runs; it can wipe another session's uncommitted work"),
        (r"\bgit\s+clean\b[^|;&]*-\w*[fdx]", "no git clean during unattended runs; it can delete another session's untracked work"),
        (r"\bgit\s+(checkout|restore)\s+[.]", "no tree-wide git checkout/restore during unattended runs; revert per-file instead"),
        (r"\bgit\s+stash\b", "no git stash during unattended runs; it can pocket another session's uncommitted work"),
    ]
    for pattern, reason in rules:
        if re.search(pattern, cmd):
            deny(reason)
    # Destructive removal = RECURSION (-r/-R/--recursive). Plain `rm -f file`
    # has a bounded blast radius and is allowed; only recursive deletes
    # outside /tmp are denied.
    if re.search(r"\brm\s+([^|;&]*\s)?(-\w*[rR]\w*|--recursive)\b", cmd) and "/tmp/" not in cmd:
        deny("recursive rm outside /tmp is blocked during unattended runs")
else:
    path = tool_input.get("file_path", "") or ""
    if "mcp-and-power-hardening" in path:
        deny("that roadmap doc is owned by another session")

sys.exit(0)
'
exit $?

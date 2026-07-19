#!/usr/bin/env bash
# PostToolUse hook: after Edit/Write on a Python file, auto-fix with
# ruff and surface any residual diagnostics immediately (exit 2 puts
# them in front of Claude at the edit, not at commit time).
#
# Silent no-op when the file isn't Python, was deleted, or ruff is
# unavailable — this hook must never wedge a session on infra issues.

set -u
ROOT="${CLAUDE_PROJECT_DIR:-.}"
RUFF="$ROOT/.venv/bin/ruff"
[ -x "$RUFF" ] || exit 0

FILE=$(python3 -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print((payload.get("tool_input") or {}).get("file_path", ""))
' 2>/dev/null)

case "$FILE" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$FILE" ] || exit 0

"$RUFF" check --fix --quiet "$FILE" >/dev/null 2>&1
"$RUFF" format --quiet "$FILE" >/dev/null 2>&1

OUT=$("$RUFF" check --quiet "$FILE" 2>&1)
if [ -n "$OUT" ]; then
  echo "ruff diagnostics remain in $FILE:" >&2
  echo "$OUT" >&2
  exit 2
fi
exit 0

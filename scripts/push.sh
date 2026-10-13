#!/usr/bin/env bash
# Gate, then push: the one way to push to main.
#
#   scripts/push.sh            # gate HEAD (skipped if already stamped green), push to origin main
#
# The gate (scripts/ci_gate.sh) runs the whole CI gate on HEAD in a throwaway
# worktree and stamps it green; the pre-push hook refuses anything unstamped.
# If origin/main moved, rebase or merge first -- the gate must run on the
# commit that will actually land.
set -euo pipefail
cd -- "$(git rev-parse --show-toplevel)"
git fetch -q origin main
head="$(git rev-parse HEAD)"
if ! git merge-base --is-ancestor origin/main "$head"; then
  echo "✗ push: HEAD does not contain origin/main; rebase or merge first, then re-run." >&2
  exit 1
fi
if [[ "$(git rev-parse origin/main)" == "$head" ]]; then
  echo "push: nothing to push"; exit 0
fi
stamp="$(git rev-parse --git-common-dir)/ci-gate/$head"
if [[ -f "$stamp" ]]; then
  echo "push: ${head:0:7} already gated: $(cut -d' ' -f2- "$stamp")"
elif git diff --name-only origin/main "$head" | grep -qvE '(\.mdx?$|^docs/|^README|^CHANGELOG|^LICENSE)'; then
  scripts/ci_gate.sh "$head" origin/main
else
  echo "push: documentation only -- no gate"
fi
git push origin "$head:refs/heads/main"
echo "push: pushed ${head:0:7}. GitHub CI is off (AGENTS.md): the gate above is the verdict."

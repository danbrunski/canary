#!/usr/bin/env bash
# Exit 0 when <base>..<head> touches a path in scripts/ml_paths.txt, i.e. when
# the `ml_training` test tier must run; exit 1 otherwise. Prints the matching
# files so a gate log says why the tier ran.
#
#   scripts/ml_changed.sh <base> [<head>]      # head defaults to HEAD
#
# The tier is not a commit gate (operator, 2026-09-23); this is what selects
# it for scripts/ci_gate.sh and .github/workflows/ci.yml.
set -euo pipefail
base="${1:?usage: ml_changed.sh <base> [<head>]}"
head="${2:-HEAD}"
root="$(git rev-parse --show-toplevel)"
mapfile -t specs < <(grep -vE '^\s*(#|$)' "$root/scripts/ml_paths.txt")
changed="$(git diff --name-only "$base" "$head" -- "${specs[@]}")"
if [[ -n "$changed" ]]; then
  echo "ml_training tier selected; ML paths changed:"
  echo "$changed" | sed 's/^/  /'
  exit 0
fi
echo "ml_training tier skipped: no path in scripts/ml_paths.txt changed"
exit 1

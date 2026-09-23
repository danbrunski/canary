#!/usr/bin/env bash
# The full CI gate on one commit, in a throwaway worktree, before anything
# reaches origin. On success it stamps that exact commit, which is what the
# pre-push hook checks. Usually run by scripts/push.sh rather than by hand.
#
#   scripts/ci_gate.sh [<commit>] [<base>]    # default: HEAD against origin/main
#
# - A worktree, not the working tree: several agent sessions share this checkout,
#   and their uncommitted edits must not decide whether *your* commits pass.
# - The venv must match constraints-dev.txt (scripts/check_dev_env.py), because
#   that file is what CI installs. Green on other versions proves nothing.
# - Python: ruff check, ruff format --check, mypy, pytest (ci.yml `test`).
#   Parallel-run failures are re-run serially once (see T01).
# - Frontend, only when <base>..<commit> touches src/dashboard/static: pnpm
#   typecheck, vitest, playwright e2e (ci.yml `frontend`).
#
# Why not inside the pre-push hook: git opens the connection to GitHub before
# the hook runs, and GitHub closed it during the ~7 min gate on 2026-09-23
# ("Connection to github.com closed by remote host"), so a green gate still
# failed the push.
set -uo pipefail
repo_root="$(git rev-parse --show-toplevel)"
venv="$repo_root/.venv/bin"
sha="$(git rev-parse --verify "${1:-HEAD}^{commit}")" || exit 2
base="$(git rev-parse --verify "${2:-origin/main}^{commit}" 2>/dev/null || true)"
range="${base:+$base..}$sha"
stamp_dir="$(git rev-parse --git-common-dir)/ci-gate"
mkdir -p "$stamp_dir"

wt="$(mktemp -d "${TMPDIR:-/tmp}/canary-gate-XXXXXX")"
cleanup() { git -C "$repo_root" worktree remove --force "$wt" >/dev/null 2>&1; rm -rf "$wt"; }
trap cleanup EXIT
git -C "$repo_root" worktree add --detach --force "$wt" "$sha" >/dev/null 2>&1 || {
  echo "ci_gate: could not create a worktree for $sha" >&2; exit 1; }
echo "ci_gate: gating ${sha:0:7} in $wt"
start=$(date +%s)
  (
    cd "$wt" || exit 1
    export PYTHONPATH="$wt"     # import this commit's src/, not the editable install's
    rc=0
    echo "==> venv matches constraints-dev.txt"
    "$venv/python" "$wt/scripts/check_dev_env.py" "$wt/constraints-dev.txt" || exit 1
    echo "==> ruff check .";          "$venv/ruff" check . || rc=1
    echo "==> ruff format --check ."; "$venv/ruff" format --check . >/dev/null || { "$venv/ruff" format --check . | tail -5; rc=1; }
    echo "==> mypy";                  "$venv/mypy" || rc=1
    [[ $rc -ne 0 ]] && exit 1
    echo "==> pytest"
    log="$wt/.prepush-pytest.log"
    "$venv/pytest" -q -rf -p no:cacheprovider >"$log" 2>&1
    prc=$?
    tail -4 "$log"
    if [[ $prc -ne 0 ]]; then
      # Several agent sessions share this laptop, and a parallel run under
      # someone else's load has failed tests that pass alone (tests/test_daemon.py
      # starts a real daemon and allows 1-5 s of wall clock; 2026-09-23 at load
      # average ~12). Re-run only the failures, serially, once. A regression
      # fails again and blocks the push; a flake passes and is named below.
      mapfile -t failed < <(sed -nE 's/^FAILED ([^ ]+).*/\1/p' "$log" | sort -u)
      if [[ ${#failed[@]} -eq 0 ]]; then
        tail -40 "$log"; exit 1          # errors/collection failures: no re-run
      fi
      echo "==> re-running ${#failed[@]} failed test(s) serially (load average: $(cut -d' ' -f1-3 /proc/loadavg))"
      if "$venv/pytest" -q -n0 -p no:cacheprovider "${failed[@]}" 2>&1 | tail -15; [[ ${PIPESTATUS[0]} -ne 0 ]]; then
        echo "✗ failed again serially: a real failure, not load" >&2
        exit 1
      fi
      echo "⚠ FLAKY UNDER LOAD -- failed in the parallel run, passed serially:" >&2
      printf '    %s\n' "${failed[@]}" >&2
      echo "  Stamping green anyway. Record them in docs/roadmap/tasks/T01-test-suite-reliability.md." >&2
      printf '%s\n' "${failed[@]}" >"$wt/.flaky"
    fi

    if git -C "$wt" diff --name-only "${range%%..*}" "$sha" 2>/dev/null | grep -q '^src/dashboard/static/' \
       || [[ "$range" != *..* ]]; then
      cd src/dashboard/static || exit 1
      echo "==> pnpm install --frozen-lockfile"; pnpm install --frozen-lockfile --prefer-offline >/dev/null || exit 1
      echo "==> pnpm typecheck"; pnpm typecheck || exit 1
      echo "==> pnpm test";      pnpm test || exit 1
      echo "==> pnpm e2e";       pnpm e2e || exit 1
    fi
  )
rc=$?
flaky="$(cat "$wt/.flaky" 2>/dev/null | tr '\n' ' ')"
if [[ $rc -ne 0 ]]; then
  echo "✗ ci_gate: ${sha:0:7} is not green -- fix it before pushing." >&2
  exit 1
fi
printf '%s green %s (%ss)%s\n' "$sha" "$(date -u +%FT%TZ)" "$(( $(date +%s) - start ))" \
  "${flaky:+ flaky-under-load: $flaky}" >"$stamp_dir/$sha"
echo "✓ ci_gate: ${sha:0:7} green -- stamped; git push will accept it"

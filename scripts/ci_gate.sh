#!/usr/bin/env bash
# The push gate on one commit, in a throwaway worktree, before anything reaches
# origin. On success it stamps that exact commit, which is what the pre-push
# hook checks. Usually run by scripts/push.sh rather than by hand.
#
# **About a minute (operator, 2026-09-24); GitHub CI runs everything.** The
# gate runs lint, types, and the whole suite except the `slow` tier -- the
# ~140 tests measured at 2 s or more (tests/slow_tests.txt), which were 3 % of
# the tests and most of the time: without them the rest ran in 86 s at load
# average 10. CI runs the slow tier, full vitest and e2e on every push, so
# after pushing, confirm CI, and a red main is fixed before anything else.
#
#   scripts/ci_gate.sh [<commit>] [<base>]    # default: HEAD against origin/main
#
# - A worktree, not the working tree: several agent sessions share this checkout,
#   and their uncommitted edits must not decide whether *your* commits pass.
# - The venv must match constraints-dev.txt (scripts/check_dev_env.py), because
#   that file is what CI installs. Green on other versions proves nothing.
# - Python: ruff check, ruff format --check, mypy (with a cache kept between
#   gates -- cold, it is 45 s of the budget), then pytest without
#   `slow`/`integration`/`ml_training`. Parallel-run
#   failures are re-run serially once (see T01). The `ml_training` tier still
#   runs here when <base>..<commit> touches scripts/ml_paths.txt, because CI
#   has no torch (operator, 2026-09-23): those pushes take minutes, not one.
# - Frontend, only when <base>..<commit> touches src/dashboard/static: pnpm
#   typecheck and `vitest related` on the changed files. Full vitest and
#   playwright e2e are CI's (ci.yml `frontend`).
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
    echo "==> mypy";                  MYPY_CACHE_DIR="$stamp_dir/mypy-cache" "$venv/mypy" || rc=1
    [[ $rc -ne 0 ]] && exit 1
    echo "==> pytest (not slow; CI runs the slow tier)"
    log="$wt/.prepush-pytest.log"
    "$venv/pytest" -q -rf -p no:cacheprovider \
      -m "not integration and not ml_training and not slow" >"$log" 2>&1
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

    # The torch training suites are not a commit gate (operator, 2026-09-23):
    # bare pytest excludes `ml_training`, and it runs here only when the range
    # touches a path in scripts/ml_paths.txt (or there is no base to diff).
    if [[ "$range" != *..* ]] || "$wt/scripts/ml_changed.sh" "${range%%..*}" "$sha"; then
      echo "==> pytest -m ml_training"
      "$venv/pytest" -q -rf -p no:cacheprovider -m ml_training 2>&1 | tail -6
      [[ ${PIPESTATUS[0]} -ne 0 ]] && { echo "✗ ml_training tier failed" >&2; exit 1; }
    fi

    mapfile -t frontend < <(git -C "$wt" diff --name-only "${range%%..*}" "$sha" 2>/dev/null \
      | sed -n 's|^src/dashboard/static/||p')
    if [[ ${#frontend[@]} -gt 0 || "$range" != *..* ]]; then
      cd src/dashboard/static || exit 1
      echo "==> pnpm install --frozen-lockfile"; pnpm install --frozen-lockfile --prefer-offline >/dev/null || exit 1
      echo "==> pnpm typecheck"; pnpm typecheck || exit 1
      echo "==> vitest related (${#frontend[@]} changed file(s)); full vitest + e2e run in CI"
      pnpm exec vitest related --run --passWithNoTests "${frontend[@]}" || exit 1
    fi
  )
rc=$?
flaky="$(cat "$wt/.flaky" 2>/dev/null | tr '\n' ' ')"
if [[ $rc -ne 0 ]]; then
  echo "✗ ci_gate: ${sha:0:7} is not green -- fix it before pushing." >&2
  exit 1
fi
elapsed=$(( $(date +%s) - start ))
printf '%s green %s (%ss)%s\n' "$sha" "$(date -u +%FT%TZ)" "$elapsed" \
  "${flaky:+ flaky-under-load: $flaky}" >"$stamp_dir/$sha"
echo "✓ ci_gate: ${sha:0:7} green in ${elapsed}s -- stamped; git push will accept it"
if [[ $elapsed -gt 120 ]]; then
  echo "  (well over the ~1 minute budget: if tests, re-measure with scripts/mark_slow_tests.py)" >&2
fi

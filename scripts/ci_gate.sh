#!/usr/bin/env bash
# The push gate on one commit, in a throwaway worktree, before anything reaches
# origin. On success it stamps that exact commit, which is what the pre-push
# hook checks. Usually run by scripts/push.sh rather than by hand.
#
# **About a minute (operator, 2026-09-24).** The gate runs lint, types, and
# the whole suite except the `slow` tier -- the tests measured at 1.5 s or
# more (tests/slow_tests.txt), a few percent of the tests and most of the
# time. GitHub CI is switched off (operator, 2026-10-13; see AGENTS.md), so
# this gate is the only one: the slow tier, full vitest and e2e run by hand
# after changing code they cover.
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
#   typecheck; ESLint and Prettier on the changed files (the whole dashboard
#   when its lint/format/TS config or dependencies changed); `vitest related`
#   on the changed files, whose failing files are re-run serially once, as
#   pytest's are (T01.10). Full lint, vitest and playwright e2e are CI's
#   (ci.yml `frontend`).
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
# Absolute: the worktree below has a .git *file*, so a relative ".git/..." path
# breaks inside it (mypy crashed trying to make its cache there, 2026-09-24).
stamp_dir="$(git rev-parse --path-format=absolute --git-common-dir)/ci-gate"
mkdir -p "$stamp_dir"

# A failed run's log lives in the worktree, which is deleted on exit, so the
# traceback of a test that flaked under load was lost (T01.13). Copy it out;
# keep the newest 20.
kept_logs="$stamp_dir/failed-logs"
keep_log() {
  mkdir -p "$kept_logs"
  local dest
  dest="$kept_logs/$(date -u +%Y%m%dT%H%M%SZ)-${sha:0:7}-$2.log"
  cp "$1" "$dest" && echo "  full log kept: $dest" >&2
  ls -1t "$kept_logs" | tail -n +21 | sed "s|^|$kept_logs/|" | xargs -r rm -f
}

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
    echo "==> pytest (not slow; run pytest -m slow by hand)"
    log="$wt/.prepush-pytest.log"
    "$venv/pytest" -q -rf -p no:cacheprovider \
      -m "not integration and not ml_training and not slow" >"$log" 2>&1
    prc=$?
    tail -4 "$log"
    if [[ $prc -ne 0 ]]; then
      keep_log "$log" pytest
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
      # Lint and format only what changed (type-aware ESLint over the whole
      # dashboard is ~40 s), unless the change can move every file's verdict.
      mapfile -t changed < <(git -C "$wt" diff --name-only --diff-filter=d "${range%%..*}" "$sha" \
        2>/dev/null | sed -n 's|^src/dashboard/static/||p')
      mapfile -t lintable < <(printf '%s\n' "${changed[@]}" | grep -E '\.(ts|tsx|js)$')
      mapfile -t formattable < <(printf '%s\n' "${changed[@]}" | grep -E '\.(ts|tsx|js|css)$')
      whole='^(eslint\.config\.js|eslint-suppressions\.json|\.prettierrc\.json|package\.json|pnpm-lock\.yaml|tsconfig.*\.json)$'
      if [[ "$range" != *..* ]] || printf '%s\n' "${frontend[@]}" | grep -qE "$whole"; then
        echo "==> eslint + prettier (whole dashboard)"
        pnpm lint || exit 1
        pnpm format:check || exit 1
      else
        if [[ ${#lintable[@]} -gt 0 ]]; then
          echo "==> eslint (${#lintable[@]} changed file(s))"
          pnpm exec eslint --max-warnings 0 "${lintable[@]}" || exit 1
        fi
        if [[ ${#formattable[@]} -gt 0 ]]; then
          echo "==> prettier --check (${#formattable[@]} changed file(s))"
          pnpm exec prettier --check "${formattable[@]}" || exit 1
        fi
      fi
      echo "==> vitest related (${#frontend[@]} changed file(s)); full vitest + e2e run in CI"
      vlog="$wt/.prepush-vitest.log"
      NO_COLOR=1 pnpm exec vitest related --run --passWithNoTests "${frontend[@]}" >"$vlog" 2>&1
      vrc=$?
      tail -6 "$vlog"
      if [[ $vrc -ne 0 ]]; then
        keep_log "$vlog" vitest
        # Same rule as the pytest step (T01.10): a vitest file that failed
        # beside every other jsdom worker is re-run alone, files one at a time,
        # once. Failing again blocks the push; passing is named as flaky.
        mapfile -t vfailed < <(sed -E 's/\x1b\[[0-9;]*m//g' "$vlog" \
          | sed -nE 's/^ *FAIL +([^ ]+\.(test|spec)\.[jt]sx?).*/\1/p' | sort -u)
        if [[ ${#vfailed[@]} -eq 0 ]]; then
          tail -40 "$vlog"; exit 1       # no failing file named: no re-run
        fi
        echo "==> re-running ${#vfailed[@]} failed vitest file(s) serially (load average: $(cut -d' ' -f1-3 /proc/loadavg))"
        if NO_COLOR=1 pnpm exec vitest run --no-file-parallelism "${vfailed[@]}" 2>&1 | tail -15; [[ ${PIPESTATUS[0]} -ne 0 ]]; then
          echo "✗ vitest failed again serially: a real failure, not load" >&2
          exit 1
        fi
        echo "⚠ FLAKY UNDER LOAD -- failed in the parallel vitest run, passed serially:" >&2
        printf '    src/dashboard/static/%s\n' "${vfailed[@]}" >&2
        echo "  Stamping green anyway. Record them in docs/roadmap/tasks/T01-test-suite-reliability.md." >&2
        printf 'src/dashboard/static/%s\n' "${vfailed[@]}" >>"$wt/.flaky"
      fi
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

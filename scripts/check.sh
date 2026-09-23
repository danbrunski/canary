#!/usr/bin/env bash
# Local CI-parity gate. Runs exactly what .github/workflows/ci.yml runs, in the
# same order, so "green locally" == "green in CI". Run before every commit/push.
# The tracked pre-commit hook runs the fast subset of this automatically.
#
#   scripts/check.sh          # full gate: ruff -> ruff format -> mypy -> pytest
#   scripts/check.sh --fast   # skip pytest (what the pre-commit hook runs)
#   scripts/check.sh --fix    # auto-apply `ruff format` + `ruff check --fix`, then gate
#   scripts/check.sh --no-env-check   # skip the venv-vs-constraints check (upgrading only)
#
# First it checks that the venv matches constraints-dev.txt, the exact versions
# CI installs. A gate run on other versions proves nothing about CI (2026-09-23:
# mypy 2.x + numpy 2.5 stubs failed CI on code the laptop's mypy 1.20 passed).
#
# CI order (ci.yml): ruff check .  ->  ruff format --check .  ->  mypy  ->  pytest -v
set -uo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
VENV=".venv/bin"

fix=0
fast=0
env_check=1
for arg in "$@"; do
  case "$arg" in
    --fix) fix=1 ;;
    --fast) fast=1 ;;
    --no-env-check) env_check=0 ;;
    *) echo "usage: scripts/check.sh [--fix] [--fast] [--no-env-check]" >&2; exit 2 ;;
  esac
done

if [[ $env_check -eq 1 ]]; then
  echo "==> venv matches constraints-dev.txt"
  "$VENV/python" "$(dirname -- "${BASH_SOURCE[0]}")/check_dev_env.py" constraints-dev.txt || exit 1
fi

if [[ $fix -eq 1 ]]; then
  echo "==> ruff format ."        ; "$VENV/ruff" format .
  echo "==> ruff check --fix ."   ; "$VENV/ruff" check --fix . || true
fi

rc=0
echo "==> ruff check ."           ; "$VENV/ruff" check .          || rc=1
echo "==> ruff format --check ."  ; "$VENV/ruff" format --check . || rc=1
echo "==> mypy"                   ; "$VENV/mypy"                  || rc=1
if [[ $rc -ne 0 ]]; then
  echo "✗ fast gate failed — fix the above, or run: scripts/check.sh --fix" >&2
  exit 1
fi
if [[ $fast -eq 1 ]]; then
  echo "✓ fast gate passed (ruff, ruff-format, mypy) — pytest skipped (--fast)"
  exit 0
fi
echo "==> pytest -v"              ; "$VENV/pytest" -v             || rc=1
if [[ $rc -ne 0 ]]; then echo "✗ pytest failed" >&2; exit 1; fi
echo "✓ ALL CI GATES PASSED (ruff, ruff-format, mypy, pytest)"

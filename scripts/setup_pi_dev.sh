#!/usr/bin/env bash
set -euo pipefail

# Fresh-machine developer bootstrap for CANary.
#
# Idempotent. Brings a clean Raspberry Pi (or any Debian box) from a bare
# git clone to a state where you can RUN and DEVELOP CANary:
#
#   1. OS packages (apt): can-utils, iproute2, git, python3-venv, and
#      nodejs/npm when the dashboard build is enabled.
#   2. corepack enable + pnpm activation  (the easy-to-miss step: corepack
#      ships disabled on Debian, so `pnpm` is not on PATH until enabled).
#   3. Dev virtualenv via scripts/install_deps.py (project + [dev] tools,
#      and CPU PyTorch by default so the LSTM path works).
#   4. canary.yaml created from canary.dev.example.yaml if absent, plus the
#      repo-local data/ and models/ directories it points at.
#   5. Browser dashboard build (pnpm install + build).
#   6. Optional verification (ruff / mypy / pytest).
#
# This is the DEVELOPER path. For the in-vehicle headless systemd service
# (system user, /etc/canary, /data/canary), use scripts/install_pi.sh.
#
# Run as the normal dev user (NOT root); apt and `corepack enable` use sudo.
#
# Re-running is safe. Note: the default CANARY_TORCH=cpu inherits
# install_deps.py's --force-reinstall, so a default re-run re-downloads the
# (large) Torch wheel. Once Torch is installed, re-run with CANARY_TORCH=none.
#
# Configurable via environment:
#   CANARY_TORCH=cpu|none|auto|cu126|...  (default: cpu)
#   CANARY_EXTRAS=dev|none|...            (default: dev)
#   CANARY_DASHBOARD=1|0                  (default: 1)
#   CANARY_APT=1|0                        (default: 1; skip OS package step)
#   CANARY_VERIFY=1|0                     (default: 0; run ruff/mypy/pytest)
#   CANARY_PNPM_VERSION=<x.y.z>           (default: from package.json)

usage() {
  sed -n '3,33p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && usage

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run as your normal dev user, not root. apt/corepack use sudo as needed." >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
STATIC_DIR="${REPO_ROOT}/src/dashboard/static"

CANARY_TORCH="${CANARY_TORCH:-cpu}"
CANARY_EXTRAS="${CANARY_EXTRAS:-dev}"
CANARY_DASHBOARD="${CANARY_DASHBOARD:-1}"
CANARY_APT="${CANARY_APT:-1}"
CANARY_VERIFY="${CANARY_VERIFY:-0}"
CANARY_PYTHON="${CANARY_PYTHON:-python3}"

# Pin pnpm to the repo's declared packageManager so the lockfile stays valid.
PNPM_VERSION="$(sed -n 's/.*"packageManager": *"pnpm@\([0-9.]*\)".*/\1/p' \
  "${STATIC_DIR}/package.json" 2>/dev/null || true)"
PNPM_VERSION="${CANARY_PNPM_VERSION:-${PNPM_VERSION:-10.29.3}}"

cd "${REPO_ROOT}"

step() { printf '\n=== %s ===\n' "$1"; }

# 1. OS packages -------------------------------------------------------------
step "OS packages"
if [[ "${CANARY_APT}" != "1" ]]; then
  echo "Skipping apt step (CANARY_APT=${CANARY_APT})."
elif ! command -v apt-get >/dev/null 2>&1; then
  echo "apt-get not found; not a Debian/Raspberry Pi OS host. Install the"
  echo "equivalents manually: git python3-venv can-utils iproute2" \
       "${CANARY_DASHBOARD:+nodejs npm}."
else
  PKGS=(git python3-venv can-utils iproute2)
  [[ "${CANARY_DASHBOARD}" == "1" ]] && PKGS+=(nodejs npm)
  echo "Installing: ${PKGS[*]}"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${PKGS[@]}"
fi

if ! command -v "${CANARY_PYTHON}" >/dev/null 2>&1; then
  echo "Python executable not found: ${CANARY_PYTHON}" >&2
  exit 1
fi

# 2. corepack / pnpm ---------------------------------------------------------
if [[ "${CANARY_DASHBOARD}" == "1" ]]; then
  step "corepack / pnpm ${PNPM_VERSION}"
  # Debian ships corepack disabled: `pnpm` is absent from PATH until enabled.
  # Skipping this is the classic silent dashboard-build failure.
  if command -v corepack >/dev/null 2>&1; then
    sudo corepack enable
    corepack prepare "pnpm@${PNPM_VERSION}" --activate
  else
    echo "corepack not found (old Node?). Install Node >= 18 with corepack." >&2
    exit 1
  fi
  pnpm --version
fi

# 3. Dev virtualenv ----------------------------------------------------------
step "Dev virtualenv (.venv)"
echo "extras=${CANARY_EXTRAS} torch=${CANARY_TORCH}"
"${CANARY_PYTHON}" scripts/install_deps.py \
  --venv "${REPO_ROOT}/.venv" \
  --extras "${CANARY_EXTRAS}" \
  --torch "${CANARY_TORCH}"

# 4. Config + runtime dirs ---------------------------------------------------
step "Config + runtime directories"
if [[ ! -f "${REPO_ROOT}/canary.yaml" ]]; then
  cp "${REPO_ROOT}/canary.dev.example.yaml" "${REPO_ROOT}/canary.yaml"
  echo "Created canary.yaml from canary.dev.example.yaml (edit as needed)."
else
  echo "Keeping existing canary.yaml."
fi
mkdir -p "${REPO_ROOT}/data/sessions" \
         "${REPO_ROOT}/data/anomalies" \
         "${REPO_ROOT}/models"
echo "Ensured: data/sessions data/anomalies models"

# 5. Dashboard build ---------------------------------------------------------
if [[ "${CANARY_DASHBOARD}" == "1" ]]; then
  step "Dashboard build"
  # No pipes here on purpose: a piped failure would be masked from `set -e`.
  ( cd "${STATIC_DIR}"
    pnpm install --frozen-lockfile
    pnpm build )
  if [[ -f "${STATIC_DIR}/dist/index.html" ]]; then
    echo "Built ${STATIC_DIR}/dist (FastAPI auto-mounts it at /)."
  else
    echo "Dashboard build did not produce dist/index.html." >&2
    exit 1
  fi
fi

# 6. Optional verification ---------------------------------------------------
if [[ "${CANARY_VERIFY}" == "1" ]]; then
  step "Verification"
  VENV_PY="${REPO_ROOT}/.venv/bin/python"
  "${VENV_PY}" -c "from pathlib import Path; from src.config import CanaryConfig; \
CanaryConfig.from_yaml(Path('canary.yaml')); print('canary.yaml: OK')"
  # Informational: do not abort the bootstrap on lint/type/test findings.
  "${VENV_PY}" -m ruff check . || echo "(ruff reported findings — see above)"
  "${VENV_PY}" -m mypy        || echo "(mypy reported findings — see above)"
  "${VENV_PY}" -m pytest -q   || echo "(pytest reported failures — see above)"
fi

cat <<EOF

=== CANary dev bootstrap complete ===

Next:
  - Edit canary.yaml if needed.
  - Bring up can0 when a Canable is attached:  see docs/can-setup.md
  - Run the daemon:        .venv/bin/python -m src.main
  - Dashboard / API:       http://<pi-hostname-or-ip>:8000/
  - Tests / lint / types:  .venv/bin/pytest  .venv/bin/ruff check .  .venv/bin/mypy

For the in-vehicle systemd service install instead, use scripts/install_pi.sh.
EOF

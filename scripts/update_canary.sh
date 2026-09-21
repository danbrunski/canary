#!/usr/bin/env bash
set -euo pipefail

# Update an installed CANary checkout and redeploy it in place.
#
# This is the private-repository transport used by the development vehicle.
# The public distribution path can replace the git pull with a verified release
# bundle while retaining the build, install, restart, and health-check steps.

usage() {
  cat <<'EOF'
Usage: sudo canary-update [--force]

Fast-forward the installed CANary checkout, build the dashboard, apply Pi
configuration and dependency changes, restart CANary, and verify /status.

The update refuses while a capture is active. Use --force only when ending
that capture is intentional.

Environment:
  CANARY_HOME       Installed checkout (default: value in canary.env, then /opt/canary)
  CANARY_UPDATE_REF Git ref to pull (default: the checkout's configured upstream)
  CANARY_STATUS_URL Health endpoint (default: http://localhost:8000/status)
EOF
}

FORCE=0
for arg in "$@"; do
  case "${arg}" in
    --force) FORCE=1 ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: ${arg}" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo canary-update" >&2
  exit 1
fi

ENV_FILE="/etc/canary/canary.env"
if [[ -z "${CANARY_HOME:-}" && -r "${ENV_FILE}" ]]; then
  CANARY_HOME="$(sed -n 's/^CANARY_HOME=//p' "${ENV_FILE}" | tail -n 1)"
fi
CANARY_HOME="${CANARY_HOME:-/opt/canary}"
STATUS_URL="${CANARY_STATUS_URL:-http://localhost:8000/status}"
CANARY_USER="${CANARY_USER:-canary}"

if [[ ! -d "${CANARY_HOME}/.git" ]]; then
  echo "CANary checkout is not a git repository: ${CANARY_HOME}" >&2
  exit 1
fi
if [[ ! -x "${CANARY_HOME}/scripts/install_pi.sh" ]]; then
  echo "CANary installer is missing: ${CANARY_HOME}/scripts/install_pi.sh" >&2
  exit 1
fi

step() { printf '\n=== %s ===\n' "$1"; }

step "Checking whether CANary can restart"
restart_args=(--check)
if [[ "${FORCE}" -eq 1 ]]; then
  restart_args+=(--force)
fi
"${CANARY_HOME}/scripts/restart_canary.sh" "${restart_args[@]}"

before="$(git -C "${CANARY_HOME}" rev-parse --short HEAD)"
tracked_changes="$(git -C "${CANARY_HOME}" status --short --untracked-files=no)"
if [[ -n "${tracked_changes}" ]]; then
  echo "Refusing to update a checkout with tracked local changes:" >&2
  printf '%s\n' "${tracked_changes}" >&2
  echo "Commit or revert those changes, then run canary-update again." >&2
  exit 1
fi

step "Fetching CANary update"
if [[ -n "${CANARY_UPDATE_REF:-}" ]]; then
  runuser -u "${CANARY_USER}" -- git -C "${CANARY_HOME}" fetch origin "${CANARY_UPDATE_REF}"
  runuser -u "${CANARY_USER}" -- git -C "${CANARY_HOME}" merge --ff-only FETCH_HEAD
else
  runuser -u "${CANARY_USER}" -- git -C "${CANARY_HOME}" pull --ff-only
fi
after="$(git -C "${CANARY_HOME}" rev-parse --short HEAD)"
echo "source: ${before} -> ${after}"

step "Building dashboard"
DASHBOARD_DIR="${CANARY_HOME}/src/dashboard/static"
if ! command -v pnpm >/dev/null 2>&1; then
  echo "pnpm is required to build the dashboard." >&2
  echo "Install Node.js + Corepack, then run: corepack enable" >&2
  exit 1
fi
runuser -u "${CANARY_USER}" -- bash -c \
  'cd "$1" && pnpm install --frozen-lockfile && pnpm build' bash "${DASHBOARD_DIR}"

step "Applying Pi configuration and Python dependencies"
CANARY_HOME="${CANARY_HOME}" bash "${CANARY_HOME}/scripts/install_pi.sh"

step "Restarting services"
restart_args=()
if [[ "${FORCE}" -eq 1 ]]; then
  restart_args+=(--force)
fi
"${CANARY_HOME}/scripts/restart_canary.sh" "${restart_args[@]}"
systemctl restart canary-mcp.service

step "Verifying daemon health"
healthy=0
for _attempt in {1..20}; do
  if curl -fsS --max-time 2 "${STATUS_URL}" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done
if [[ "${healthy}" -ne 1 ]]; then
  echo "Update installed, but CANary did not answer at ${STATUS_URL}." >&2
  echo "Inspect: sudo journalctl -u canary -n 100 --no-pager" >&2
  exit 1
fi

echo "CANary update complete (${after})."

#!/usr/bin/env bash
set -euo pipefail

# Update an installed CANary checkout and redeploy it in place.
#
# This is the private-repository transport used by the development vehicle.
# The public distribution path can replace the git pull with a verified release
# bundle while retaining the build, install, restart, and health-check steps.
#
# Tested offline by tests/test_update_canary_script.py, which runs this file
# against a throwaway git origin with shimmed systemctl/curl/pnpm/runuser. The
# CANARY_UPDATE_* environment knobs below exist for that harness; nothing on
# the Pi sets them.

usage() {
  cat <<'EOF'
Usage: sudo canary-update [--force] [--full] [--no-rollback]

Fast-forward the installed CANary checkout, then do only the work the change
needs (dashboard build, Python dependencies, Pi configuration), restart
CANary through scripts/restart_canary.sh, and verify /status. If the new code
does not come up, the checkout is reset to the commit it was on and redeployed.

models/ belongs to the Pi, not to git: artefacts arrive with their manifests
(canaryctl models upload, scp) and the daemon retrains in place, so local
changes there are expected. They never block an update, and neither the pull
nor a rollback changes anything under models/.

Options:
  --force        Restart even if a capture or ECU backup is active (ends it).
  --full         Run every step regardless of what changed: dashboard build,
                 pip install, install_pi.sh. First installs and "something is
                 wrong" runs use this.
  --no-rollback  Leave a failed update in place for debugging.

Exit codes:
  0  updated (or already current) and healthy
  1  setup error (not root, missing checkout or installer)
  2  usage error
  3  refused: a capture or ECU backup is active (from restart_canary.sh --check)
  4  refused: the checkout has tracked local changes (outside models/)
  5  fetch or fast-forward failed; nothing was changed
  6  new code failed its health check; rolled back to the previous commit,
     which is healthy again
  7  new code failed its health check AND the rollback did not come up;
     inspect: sudo journalctl -u canary -n 100 --no-pager
  8  new code failed its health check and was left in place (--no-rollback,
     or there was nothing older to roll back to)

Environment:
  CANARY_HOME                 Installed checkout (default: value in canary.env, then /opt/canary)
  CANARY_UPDATE_REF           Git ref to pull (default: the checkout's configured upstream)
  CANARY_STATUS_URL           Health endpoint (default: http://localhost:8000/status)
  CANARY_HEALTH_WAIT_SECONDS  How long to wait for /status after the restart (default: 90)
EOF
}

# Exit codes. Documented in usage() and docs/pi-setup.md; drive.md reads them.
EXIT_SETUP=1
EXIT_USAGE=2
EXIT_CAPTURE=3
EXIT_DIRTY=4
EXIT_FETCH=5
EXIT_ROLLED_BACK=6
EXIT_ROLLBACK_FAILED=7
EXIT_LEFT_BROKEN=8

FORCE=0
FULL=0
NO_ROLLBACK=0
for arg in "$@"; do
  case "${arg}" in
    --force) FORCE=1 ;;
    --full) FULL=1 ;;
    --no-rollback) NO_ROLLBACK=1 ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: ${arg}" >&2
      usage >&2
      exit "${EXIT_USAGE}"
      ;;
  esac
done

# The test harness cannot run as root. Nothing else sets this.
if [[ "${EUID}" -ne 0 && "${CANARY_UPDATE_ALLOW_UNPRIVILEGED:-0}" != "1" ]]; then
  echo "Run with sudo: sudo canary-update" >&2
  exit "${EXIT_SETUP}"
fi

ENV_FILE="${CANARY_ENV:-/etc/canary/canary.env}"
if [[ -z "${CANARY_HOME:-}" && -r "${ENV_FILE}" ]]; then
  CANARY_HOME="$(sed -n 's/^CANARY_HOME=//p' "${ENV_FILE}" | tail -n 1)"
fi
CANARY_HOME="${CANARY_HOME:-/opt/canary}"
STATUS_URL="${CANARY_STATUS_URL:-http://localhost:8000/status}"
CANARY_USER="${CANARY_USER:-canary}"
VENV_PYTHON="${CANARY_HOME}/.venv/bin/python"
# `systemctl show` echoes MemoryMax even when the kernel has the memory
# controller disabled; only the cgroup file proves the limit is live.
CGROUP_DIR="${CANARY_CGROUP_DIR:-/sys/fs/cgroup/system.slice/canary.service}"
# A restart takes ~25 s to answer on the Pi 5 (2026-09-22: 24 s), so the old
# 20 s window reported a healthy deploy as a failure. Give it 90 s.
HEALTH_WAIT_SECONDS="${CANARY_HEALTH_WAIT_SECONDS:-90}"
HEALTH_POLL_SECONDS="${CANARY_HEALTH_POLL_SECONDS:-1}"
STARTED_AT="${CANARY_UPDATE_STARTED_AT:-$(date +%s)}"

if [[ ! -d "${CANARY_HOME}/.git" ]]; then
  echo "CANary checkout is not a git repository: ${CANARY_HOME}" >&2
  exit "${EXIT_SETUP}"
fi
if [[ ! -x "${CANARY_HOME}/scripts/install_pi.sh" ]]; then
  echo "CANary installer is missing: ${CANARY_HOME}/scripts/install_pi.sh" >&2
  exit "${EXIT_SETUP}"
fi
# When the dashboard needs building, and the build: shared with install_pi.sh.
# Read from the checkout, so a change to it re-executes like a change to this
# file does (below).
DASHBOARD_LIB="${CANARY_HOME}/scripts/lib/dashboard_build.sh"
if [[ ! -r "${DASHBOARD_LIB}" ]]; then
  echo "CANary dashboard build helper is missing: ${DASHBOARD_LIB}" >&2
  exit "${EXIT_SETUP}"
fi
# shellcheck source=lib/dashboard_build.sh
source "${DASHBOARD_LIB}"

# The `=== <step> ===` lines and the summary block below are also the progress
# feed of an update started from the dashboard: src/control/update_runner.py
# relays each step to /run/canary/update_status as it starts, and reads the
# summary's `key: value` lines. tests/test_update_canary_script.py pins both
# (test_runner_relays_every_step_and_the_summary).
step() { printf '\n=== %s ===\n' "$1"; }

# ---------------------------------------------------------------------------
# Summary block. Every run ends with one; drive.md reads it.
# ---------------------------------------------------------------------------
before=""
after=""
SUMMARY_STEPS=()
SUMMARY_RESTART="not attempted"
SUMMARY_HEALTH="not checked"
SUMMARY_MEMORY="not checked"
SUMMARY_ROLLBACK="none"
SUMMARY_RESULT=""

finish() {
  local code="$1"
  local elapsed=$(( $(date +%s) - STARTED_AT ))
  printf '\n=== canary-update summary ===\n'
  if [[ -n "${before}" ]]; then
    echo "source: ${before} -> ${after:-${before}}"
  fi
  local line
  for line in "${SUMMARY_STEPS[@]+"${SUMMARY_STEPS[@]}"}"; do
    echo "${line}"
  done
  echo "restart: ${SUMMARY_RESTART}"
  echo "health: ${SUMMARY_HEALTH}"
  echo "memory limits: ${SUMMARY_MEMORY}"
  echo "rollback: ${SUMMARY_ROLLBACK}"
  echo "result: ${SUMMARY_RESULT} (exit ${code})"
  echo "total: ${elapsed} s"
  exit "${code}"
}

# ---------------------------------------------------------------------------
# models/ belongs to the Pi. A manifest describes the binary beside it, and the
# binary never travels by git (it is gitignored; it arrives by `canaryctl models
# upload` or scp, or the daemon retrains it), so a pulled manifest is one that
# does not match the binary on disk: the 2026-07-26 blind IF was manifests from
# a pull beside binaries two weeks older. A retrain on the Pi rewrites the
# tracked manifest, which is why a dirty models/ is normal and not refused.
#
# Since S03 (2026-10-04) nothing under models/ is tracked and the Pi keeps its
# models in daemon.state_dir (/data/canary/models), so keep_models is a no-op
# on a current tree. It stays for the pull that crosses the untracking commit
# (which would otherwise delete the two manifests it removed from git) and for
# a rollback to an older commit that tracked them.
# ---------------------------------------------------------------------------
MODELS_PATH="models"

keep_models() {
  # Run "$@" (a git command that moves HEAD) and put every path git tracks
  # under models/ back exactly as it was: local edits kept, anything the
  # command added removed, anything it deleted restored.
  local snap rc=0 restore_rc=0 path
  snap="$(mktemp -d)"
  git -C "${CANARY_HOME}" ls-files -z -- "${MODELS_PATH}" >"${snap}/tracked"
  : >"${snap}/present"
  while IFS= read -r -d '' path; do
    if [[ -e "${CANARY_HOME}/${path}" ]]; then
      printf '%s\0' "${path}" >>"${snap}/present"
    fi
  done <"${snap}/tracked"
  tar -C "${CANARY_HOME}" --null -T "${snap}/present" -cpf "${snap}/models.tar"
  if [[ -n "$(git -C "${CANARY_HOME}" status --porcelain --untracked-files=no -- "${MODELS_PATH}")" ]]; then
    # A dirty tracked file would stop a fast-forward that touches it.
    runuser -u "${CANARY_USER}" -- git -C "${CANARY_HOME}" checkout -q HEAD -- "${MODELS_PATH}" \
      || restore_rc=$?
  fi

  if [[ "${restore_rc}" -eq 0 ]]; then
    "$@" || rc=$?
  fi

  local -A was_present=()
  while IFS= read -r -d '' path; do
    was_present["${path}"]=1
  done <"${snap}/present"
  while IFS= read -r -d '' path; do
    if [[ -z "${was_present["${path}"]:-}" ]]; then
      rm -f -- "${CANARY_HOME:?}/${path}" || restore_rc=$?
    fi
  done < <(git -C "${CANARY_HOME}" ls-files -z -- "${MODELS_PATH}")
  tar -C "${CANARY_HOME}" -xpf "${snap}/models.tar" || restore_rc=$?

  if [[ "${restore_rc}" -ne 0 ]]; then
    echo "Could not keep ${MODELS_PATH}/ as it was; the pre-update copy is ${snap}/models.tar" >&2
    return 1
  fi
  rm -rf -- "${snap}"
  return "${rc}"
}

pull_update() {
  if [[ -n "${CANARY_UPDATE_REF:-}" ]]; then
    runuser -u "${CANARY_USER}" -- git -C "${CANARY_HOME}" fetch origin "${CANARY_UPDATE_REF}" \
      && runuser -u "${CANARY_USER}" -- git -C "${CANARY_HOME}" merge --ff-only FETCH_HEAD
  else
    runuser -u "${CANARY_USER}" -- git -C "${CANARY_HOME}" pull --ff-only
  fi
}

# ---------------------------------------------------------------------------
# Refusals. First, unconditional, and the only two.
# ---------------------------------------------------------------------------
step "Checking whether CANary can restart"
restart_args=(--check)
if [[ "${FORCE}" -eq 1 ]]; then
  restart_args+=(--force)
fi
rc=0
"${CANARY_HOME}/scripts/restart_canary.sh" "${restart_args[@]}" || rc=$?
if [[ "${rc}" -ne 0 ]]; then
  SUMMARY_RESULT="refused: capture or ECU backup active"
  if [[ "${rc}" -ne "${EXIT_CAPTURE}" ]]; then
    SUMMARY_RESULT="refused: restart_canary.sh --check exited ${rc}"
  fi
  finish "${rc}"
fi

tracked_changes="$(git -C "${CANARY_HOME}" status --short --untracked-files=no -- . ":(exclude)${MODELS_PATH}")"
if [[ -n "${tracked_changes}" ]]; then
  echo "Refusing to update a checkout with tracked local changes:" >&2
  printf '%s\n' "${tracked_changes}" >&2
  echo "Commit or revert those changes, then run canary-update again." >&2
  SUMMARY_RESULT="refused: tracked local changes"
  finish "${EXIT_DIRTY}"
fi

# ---------------------------------------------------------------------------
# Fetch. A re-executed copy (see below) inherits before/after instead.
# ---------------------------------------------------------------------------
if [[ -n "${CANARY_UPDATE_REEXEC:-}" ]]; then
  before="${CANARY_UPDATE_REEXEC_BEFORE:?re-exec marker set without CANARY_UPDATE_REEXEC_BEFORE}"
  after="$(git -C "${CANARY_HOME}" rev-parse --short HEAD)"
  step "Running as the updated updater"
  echo "re-executed from ${after}; fetch already done (${before} -> ${after})"
else
  before="$(git -C "${CANARY_HOME}" rev-parse --short HEAD)"
  step "Fetching CANary update"
  rc=0
  keep_models pull_update || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    echo "Fetch failed (exit ${rc}); the checkout is unchanged at ${before}." >&2
    after="${before}"
    SUMMARY_RESULT="fetch failed"
    finish "${EXIT_FETCH}"
  fi
  after="$(git -C "${CANARY_HOME}" rev-parse --short HEAD)"
  echo "source: ${before} -> ${after}"

  # The update may have replaced this very script. bash keeps reading the
  # copy it opened, so the rest of an update would run the OLD updater
  # (2026-09-22: the first run after 119527d printed the old health failure
  # while the daemon was healthy). Hand over to the new copy now, once, with
  # the fetch already done. The dashboard helper it sourced counts as part of
  # it: the functions in memory are the old ones. So do install_pi.sh's three
  # sections (lib/install_*.sh, R30.11): the updater does not source them, but
  # a change to one is a change to the installer, which the new copy runs.
  self_changed=""
  if [[ "${before}" != "${after}" ]]; then
    self_changed="$(git -C "${CANARY_HOME}" diff --name-only "${before}" "${after}" \
      | grep -xE 'scripts/update_canary\.sh|scripts/lib/(dashboard_build|install_gps_time|install_hat|install_network)\.sh' | head -n 1 || true)"
  fi
  if [[ -n "${self_changed}" ]]; then
    echo "${self_changed} changed in ${before} -> ${after}; re-executing the new copy"
    export CANARY_UPDATE_REEXEC="${after}"
    export CANARY_UPDATE_REEXEC_BEFORE="${before}"
    export CANARY_UPDATE_STARTED_AT="${STARTED_AT}"
    exec bash "${CANARY_HOME}/scripts/update_canary.sh" "$@"
  fi
fi

# ---------------------------------------------------------------------------
# Steps, decided from the diff and the artefacts, never from "looks fine".
# ---------------------------------------------------------------------------
changed_between() {
  # Paths that differ between two commits; empty when they are the same.
  if [[ "$1" == "$2" ]]; then
    return 0
  fi
  git -C "${CANARY_HOME}" diff --name-only "$1" "$2"
}

# Files install_pi.sh installs somewhere outside the checkout, or runs.
# The HAT overlay sources are: install_pi.sh compiles them into /boot/firmware,
# and picks the GNSS UART from the revision table (hat_revisions.json, hat_rev.py).
# lib/install_*.sh are install_pi.sh's own sections (GPS and time, HAT, network).
INSTALL_PI_INPUTS='^(scripts/install_pi\.sh|scripts/lib/install_[a-z_]*\.sh|scripts/install_deps\.py|scripts/update_canary\.sh|systemd/|canary\.pi\.example\.yaml|hardware/canary-hat-firmware/([^/]*-overlay\.dts|hat_revisions\.json|hat_rev\.py))'
PIP_INPUTS='^(pyproject\.toml|requirements[^/]*\.txt|uv\.lock|poetry\.lock|pdm\.lock)$'

decide_dashboard() {
  local changed="$1"
  local stale
  if [[ "${FULL}" -eq 1 ]]; then
    echo "ran (--full)"
  elif grep -q '^src/dashboard/static/' <<<"${changed}"; then
    echo "ran (src/dashboard/static changed)"
  elif stale="$(dashboard_stale_reason "${CANARY_HOME}")" && [[ -n "${stale}" ]]; then
    echo "ran (${stale})"
  else
    echo "skipped (no dashboard source change; dist/ is current)"
  fi
}

decide_install() {
  local changed="$1"
  if [[ "${FULL}" -eq 1 ]]; then
    echo "ran (--full)"
  elif [[ ! -x "${VENV_PYTHON}" ]]; then
    echo "ran (.venv missing)"
  elif grep -qE "${INSTALL_PI_INPUTS}" <<<"${changed}"; then
    echo "ran ($(grep -E "${INSTALL_PI_INPUTS}" <<<"${changed}" | head -n 1) changed)"
  else
    echo "skipped (no installer, systemd or installed-file change)"
  fi
}

decide_pip() {
  local changed="$1"
  if [[ "${FULL}" -eq 1 ]]; then
    echo "ran (--full)"
  elif grep -qE "${PIP_INPUTS}" <<<"${changed}"; then
    echo "ran ($(grep -E "${PIP_INPUTS}" <<<"${changed}" | head -n 1) changed)"
  elif ! "${VENV_PYTHON}" -m pip check >/dev/null 2>&1; then
    echo "ran (pip check failed)"
  else
    echo "skipped (no dependency change; pip check passes)"
  fi
}

run_dashboard_build() {
  dashboard_build "${CANARY_HOME}" "${CANARY_USER}"
}

run_pip_install() {
  runuser -u "${CANARY_USER}" -- "${VENV_PYTHON}" -m pip install -e "${CANARY_HOME}"
}

run_install_pi() {
  CANARY_HOME="${CANARY_HOME}" bash "${CANARY_HOME}/scripts/install_pi.sh"
}

# apply_steps <from> <to> <label>: do the work <from> -> <to> needs, record one
# summary line per step. Used forward, and again for the rollback.
apply_steps() {
  local from="$1" to="$2" label="$3"
  local changed dashboard install pip
  changed="$(changed_between "${from}" "${to}")"
  if [[ "${from}" == "${to}" ]]; then
    echo "already at ${to}; only checking artefacts and restarting"
  fi

  dashboard="$(decide_dashboard "${changed}")"
  install="$(decide_install "${changed}")"
  local run_pip=0
  if [[ "${install}" == ran* ]]; then
    # install_pi.sh runs install_deps.py itself; no separate pip step.
    pip="ran (by install_pi.sh)"
  else
    pip="$(decide_pip "${changed}")"
    if [[ "${pip}" == ran* ]]; then
      run_pip=1
    fi
  fi

  step "Dashboard build (${label})"
  echo "dashboard build: ${dashboard}"
  SUMMARY_STEPS+=("${label}dashboard build: ${dashboard}")
  if [[ "${dashboard}" == ran* ]]; then
    run_dashboard_build || return $?
  fi

  step "Python dependencies (${label})"
  echo "pip install: ${pip}"
  SUMMARY_STEPS+=("${label}pip install: ${pip}")
  if [[ "${run_pip}" -eq 1 ]]; then
    run_pip_install || return $?
  fi

  step "Pi configuration (${label})"
  echo "install_pi.sh: ${install}"
  SUMMARY_STEPS+=("${label}install_pi.sh: ${install}")
  if [[ "${install}" == ran* ]]; then
    run_install_pi || return $?
  fi
}

restart_services() {
  # Only ever through restart_canary.sh: it is the one place that knows
  # whether a capture or an ECU backup would die.
  local args=()
  if [[ "${FORCE}" -eq 1 ]]; then
    args+=(--force)
  fi
  "${CANARY_HOME}/scripts/restart_canary.sh" "${args[@]+"${args[@]}"}" || return $?
  systemctl restart canary-mcp.service || return $?
  # canary-provision runs this checkout's src/provisioning too (WiFi over
  # Bluetooth, and the socket the dashboard's WiFi page asks). Left running,
  # an update served the new page against the old service, which answered
  # 503 until someone restarted it by hand (2026-11-07, N5). Not fatal: the
  # daemon is what the health check and the rollback are about.
  systemctl restart canary-provision.service \
    || echo "WARNING: canary-provision did not restart; WiFi setup and the WiFi page may be stale" >&2
}

# wait_healthy: poll /status; sets HEALTH_SECONDS to the wait it took.
HEALTH_SECONDS=0
wait_healthy() {
  local started
  started="$(date +%s)"
  while :; do
    if curl -fsS --max-time 2 "${STATUS_URL}" >/dev/null 2>&1; then
      HEALTH_SECONDS=$(( $(date +%s) - started ))
      return 0
    fi
    if (( $(date +%s) - started >= HEALTH_WAIT_SECONDS )); then
      HEALTH_SECONDS=$(( $(date +%s) - started ))
      return 1
    fi
    sleep "${HEALTH_POLL_SECONDS}"
  done
}

# ---------------------------------------------------------------------------
# Forward
# ---------------------------------------------------------------------------
healthy=0
deploy_rc=0
apply_steps "${before}" "${after}" "" || deploy_rc=$?
if [[ "${deploy_rc}" -ne 0 ]]; then
  echo "Deploy step failed (exit ${deploy_rc}) before the restart." >&2
  SUMMARY_RESTART="not attempted (deploy step failed, exit ${deploy_rc})"
  SUMMARY_HEALTH="not checked"
else
  step "Restarting services"
  restart_rc=0
  restart_services || restart_rc=$?
  if [[ "${restart_rc}" -eq 0 ]]; then
    SUMMARY_RESTART="ok"
  else
    SUMMARY_RESTART="FAILED (exit ${restart_rc})"
    echo "Restart failed (exit ${restart_rc})." >&2
  fi

  step "Verifying daemon health"
  if [[ "${restart_rc}" -eq 0 ]] && wait_healthy; then
    healthy=1
    SUMMARY_HEALTH="ok after ${HEALTH_SECONDS} s"
    echo "${STATUS_URL} answered after ${HEALTH_SECONDS} s"
  else
    SUMMARY_HEALTH="FAILED (no answer from ${STATUS_URL} within ${HEALTH_SECONDS} s)"
    echo "CANary did not answer at ${STATUS_URL} within ${HEALTH_WAIT_SECONDS} s." >&2
  fi
fi

# ---------------------------------------------------------------------------
# Rollback: back to the recorded <before>, never anything older.
# ---------------------------------------------------------------------------
if [[ "${healthy}" -ne 1 ]]; then
  if [[ "${NO_ROLLBACK}" -eq 1 ]]; then
    SUMMARY_ROLLBACK="skipped (--no-rollback); ${after} left in place"
    SUMMARY_RESULT="update failed; left in place for debugging"
    echo "Inspect: sudo journalctl -u canary -n 100 --no-pager" >&2
    finish "${EXIT_LEFT_BROKEN}"
  fi
  if [[ "${before}" == "${after}" ]]; then
    SUMMARY_ROLLBACK="nothing to roll back to (already at ${before})"
    SUMMARY_RESULT="restart failed; no older commit to roll back to"
    echo "Inspect: sudo journalctl -u canary -n 100 --no-pager" >&2
    finish "${EXIT_LEFT_BROKEN}"
  fi

  step "Rolling back ${after} -> ${before}"
  # Safe: the updater refused a dirty tracked tree before fetching, so the
  # only thing reset discards is the fast-forward it just made; models/ is
  # the Pi's and keep_models puts it back.
  rollback_rc=0
  keep_models runuser -u "${CANARY_USER}" -- git -C "${CANARY_HOME}" reset --hard "${before}" \
    || rollback_rc=$?
  if [[ "${rollback_rc}" -ne 0 ]]; then
    SUMMARY_ROLLBACK="FAILED (git reset --hard ${before} exited ${rollback_rc})"
    SUMMARY_RESULT="update failed and rollback failed"
    finish "${EXIT_ROLLBACK_FAILED}"
  fi
  apply_steps "${after}" "${before}" "rollback " || rollback_rc=$?
  if [[ "${rollback_rc}" -ne 0 ]]; then
    SUMMARY_ROLLBACK="FAILED (redeploying ${before} exited ${rollback_rc})"
    SUMMARY_RESULT="update failed and rollback failed"
    finish "${EXIT_ROLLBACK_FAILED}"
  fi

  step "Restarting services (rollback)"
  # A restart refusal here is not fatal on its own: if the old daemon is
  # still up, the health check below says so.
  restart_services || echo "rollback restart exited $?; checking health anyway" >&2

  step "Verifying daemon health (rollback)"
  if wait_healthy; then
    SUMMARY_ROLLBACK="rolled back to ${before}; healthy after ${HEALTH_SECONDS} s"
    SUMMARY_RESULT="update failed; rolled back to ${before}"
    echo "${STATUS_URL} answered after ${HEALTH_SECONDS} s on ${before}"
    finish "${EXIT_ROLLED_BACK}"
  fi
  SUMMARY_ROLLBACK="rolled back to ${before}; still no answer within ${HEALTH_SECONDS} s"
  SUMMARY_RESULT="update failed and rollback failed"
  echo "Inspect: sudo journalctl -u canary -n 100 --no-pager" >&2
  finish "${EXIT_ROLLBACK_FAILED}"
fi

# ---------------------------------------------------------------------------
# Memory limits
# ---------------------------------------------------------------------------
step "Verifying memory limits are enforced"
if [[ -r "${CGROUP_DIR}/memory.max" ]]; then
  SUMMARY_MEMORY="enforced (memory.max $(cat "${CGROUP_DIR}/memory.max"), memory.swap.max $(cat "${CGROUP_DIR}/memory.swap.max" 2>/dev/null || echo '?'))"
  echo "${SUMMARY_MEMORY}"
else
  SUMMARY_MEMORY="NOT enforced (${CGROUP_DIR}/memory.max missing)"
  echo "WARNING: ${CGROUP_DIR}/memory.max does not exist: MemoryMax/MemorySwapMax" >&2
  echo "are NOT enforced. install_pi.sh adds cgroup_enable=memory to the kernel" >&2
  echo "command line; reboot the Pi (while parked, no capture) to activate it." >&2
fi

if [[ "${before}" == "${after}" ]]; then
  SUMMARY_RESULT="already at ${after}; restarted"
else
  SUMMARY_RESULT="updated"
fi
echo "CANary update complete (${after})."
finish 0

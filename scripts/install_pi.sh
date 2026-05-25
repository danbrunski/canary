#!/usr/bin/env bash
set -euo pipefail

# Idempotent Raspberry Pi install helper for an already-cloned CANary repo.
#
# Defaults are intentionally runtime-light: base Python dependencies only and
# no Torch. Set CANARY_TORCH=cpu if you want LSTM inference on the Pi.

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo scripts/install_pi.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

CANARY_USER="canary"
CANARY_GROUP="canary"
CANARY_HOME="${CANARY_HOME:-${REPO_ROOT}}"
CANARY_CONFIG_DIR="${CANARY_CONFIG_DIR:-/etc/canary}"
CANARY_CONFIG="${CANARY_CONFIG:-${CANARY_CONFIG_DIR}/canary.yaml}"
CANARY_ENV="${CANARY_ENV:-${CANARY_CONFIG_DIR}/canary.env}"
CANARY_CONTROL_TOKEN_FILE="${CANARY_CONTROL_TOKEN_FILE:-${CANARY_CONFIG_DIR}/control.token}"
CANARY_DATA_DIR="${CANARY_DATA_DIR:-/data/canary}"
CANARY_PYTHON="${CANARY_PYTHON:-python3}"
CANARY_EXTRAS="${CANARY_EXTRAS:-none}"
CANARY_TORCH="${CANARY_TORCH:-none}"
CANARY_SERVICE="${CANARY_SERVICE:-/etc/systemd/system/canary.service}"

if [[ "${CANARY_HOME}" != "${REPO_ROOT}" ]]; then
  echo "CANARY_HOME must point at this clone for the in-place install." >&2
  echo "  CANARY_HOME=${CANARY_HOME}" >&2
  echo "  clone=${REPO_ROOT}" >&2
  exit 1
fi

if ! command -v "${CANARY_PYTHON}" >/dev/null 2>&1; then
  echo "Python executable not found: ${CANARY_PYTHON}" >&2
  exit 1
fi

if ! getent group "${CANARY_GROUP}" >/dev/null 2>&1; then
  groupadd --system "${CANARY_GROUP}"
fi

if ! id -u "${CANARY_USER}" >/dev/null 2>&1; then
  useradd --system --gid "${CANARY_GROUP}" --home-dir "${CANARY_HOME}" --create-home \
    --shell /usr/sbin/nologin "${CANARY_USER}"
fi

install -d -m 0755 "${CANARY_CONFIG_DIR}"
install -d -m 0755 "${CANARY_DATA_DIR}" "${CANARY_DATA_DIR}/sessions" "${CANARY_DATA_DIR}/anomalies"
# canary-mcp writes its audit log here as user ``canary``; pre-create so
# ControlAuditLogger doesn't try to ``mkdir`` under a path it can't reach.
install -d -m 0750 "${CANARY_DATA_DIR}/audit"

chown -R "${CANARY_USER}:${CANARY_GROUP}" "${CANARY_HOME}" "${CANARY_DATA_DIR}"

if [[ ! -f "${CANARY_CONFIG}" ]]; then
  install -m 0644 "${CANARY_HOME}/canary.pi.example.yaml" "${CANARY_CONFIG}"
  echo "Created ${CANARY_CONFIG}; edit it before starting the service."
else
  echo "Keeping existing ${CANARY_CONFIG}."
fi

if [[ ! -f "${CANARY_CONTROL_TOKEN_FILE}" ]]; then
  "${CANARY_PYTHON}" -c 'import secrets; print(secrets.token_urlsafe(32))' \
    >"${CANARY_CONTROL_TOKEN_FILE}"
  chown root:"${CANARY_GROUP}" "${CANARY_CONTROL_TOKEN_FILE}"
  chmod 0640 "${CANARY_CONTROL_TOKEN_FILE}"
  echo "Created ${CANARY_CONTROL_TOKEN_FILE}; export it as CANARY_CONTROL_TOKEN from your laptop."
else
  echo "Keeping existing ${CANARY_CONTROL_TOKEN_FILE}."
fi

cat >"${CANARY_ENV}" <<EOF
CANARY_HOME=${CANARY_HOME}
CANARY_CONFIG=${CANARY_CONFIG}
CANARY_DATA_DIR=${CANARY_DATA_DIR}
CANARY_CONTROL_TOKEN=$(cat "${CANARY_CONTROL_TOKEN_FILE}")
CANARY_MCP_AUDIT_PATH=${CANARY_DATA_DIR}/audit/mcp.jsonl
EOF
chown root:"${CANARY_GROUP}" "${CANARY_ENV}"
chmod 0640 "${CANARY_ENV}"

install -m 0644 "${CANARY_HOME}/systemd/canary.service" "${CANARY_SERVICE}"
install -m 0644 "${CANARY_HOME}/systemd/canary-mcp.service" \
  "$(dirname "${CANARY_SERVICE}")/canary-mcp.service"

# Let the canary user restart the services without a password, so model/code
# deploys (and the MCP control plane) can roll the daemon without an interactive
# sudo. Scoped to exactly the restart/try-restart verbs for these two units —
# nothing else. Validated with `visudo -c`; a malformed sudoers file would lock
# out sudo, so we discard it on failure.
SYSTEMCTL="$(command -v systemctl)"
CANARY_SUDOERS="/etc/sudoers.d/canary-restart"
cat >"${CANARY_SUDOERS}" <<EOF
${CANARY_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} restart canary.service, ${SYSTEMCTL} restart canary, ${SYSTEMCTL} restart canary-mcp.service, ${SYSTEMCTL} restart canary-mcp, ${SYSTEMCTL} try-restart canary.service, ${SYSTEMCTL} try-restart canary-mcp.service
EOF
chmod 0440 "${CANARY_SUDOERS}"
if ! visudo -cf "${CANARY_SUDOERS}" >/dev/null; then
  echo "WARNING: generated ${CANARY_SUDOERS} failed validation; removing it" >&2
  rm -f "${CANARY_SUDOERS}"
fi

cd "${CANARY_HOME}"
runuser -u "${CANARY_USER}" -- "${CANARY_PYTHON}" scripts/install_deps.py \
  --venv "${CANARY_HOME}/.venv" \
  --extras "${CANARY_EXTRAS}" \
  --torch "${CANARY_TORCH}"

systemctl daemon-reload
systemctl enable canary.service
systemctl enable canary-mcp.service

cat <<EOF
CANary Pi install complete.

Next:
  1. Edit ${CANARY_CONFIG}
  2. Bring up can0 for your chosen OBD/CAN mode; see docs/can-setup.md
  3. Start with: sudo systemctl start canary canary-mcp
  4. Watch logs: sudo journalctl -u canary -f  (or -u canary-mcp -f)
  5. Export the laptop token: export CANARY_CONTROL_TOKEN="$(cat "${CANARY_CONTROL_TOKEN_FILE}")"
  6. Open: http://<pi-hostname-or-ip>:8000/status
  7. MCP server: http://<pi-hostname-or-ip>:8001/mcp (see docs/mcp-server.md)
EOF

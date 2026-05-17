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

chown -R "${CANARY_USER}:${CANARY_GROUP}" "${CANARY_HOME}" "${CANARY_DATA_DIR}"

if [[ ! -f "${CANARY_CONFIG}" ]]; then
  install -m 0644 "${CANARY_HOME}/canary.pi.example.yaml" "${CANARY_CONFIG}"
  echo "Created ${CANARY_CONFIG}; edit it before starting the service."
else
  echo "Keeping existing ${CANARY_CONFIG}."
fi

cat >"${CANARY_ENV}" <<EOF
CANARY_HOME=${CANARY_HOME}
CANARY_CONFIG=${CANARY_CONFIG}
EOF
chmod 0644 "${CANARY_ENV}"

install -m 0644 "${CANARY_HOME}/systemd/canary.service" "${CANARY_SERVICE}"

cd "${CANARY_HOME}"
runuser -u "${CANARY_USER}" -- "${CANARY_PYTHON}" scripts/install_deps.py \
  --venv "${CANARY_HOME}/.venv" \
  --extras "${CANARY_EXTRAS}" \
  --torch "${CANARY_TORCH}"

systemctl daemon-reload
systemctl enable canary.service

cat <<EOF
CANary Pi install complete.

Next:
  1. Edit ${CANARY_CONFIG}
  2. Bring up can0 for your chosen OBD/CAN mode; see docs/can-setup.md
  3. Start with: sudo systemctl start canary
  4. Watch logs: sudo journalctl -u canary -f
  5. Open: http://<pi-hostname-or-ip>:8000/status
EOF

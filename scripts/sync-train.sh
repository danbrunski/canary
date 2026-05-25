#!/usr/bin/env bash
set -euo pipefail

# Laptop <-> Pi retrain round-trip.
#
# The Pi captures sessions but OOMs / is slow at training. This script does the
# recurring round-trip so a retrain is one command from a CUDA laptop:
#
#   scripts/sync-train.sh pull    # rsync Pi sessions + scp Pi's live canary.yaml
#   scripts/sync-train.sh train   # retrain IF + LSTM (CUDA) and refit behavior models
#   scripts/sync-train.sh push    # rsync artifacts back; Pi ModelManager hot-swaps
#   scripts/sync-train.sh all     # pull -> train -> push (default)
#
# NON-NEGOTIABLE preprocessing parity: training MUST use the Pi's exact
# preprocessing block. We pull /etc/canary/canary.yaml verbatim to
# ./canary.laptop.yaml and train against THAT file. `assert_preprocessing`
# refuses to train if the pulled config is missing/unparseable or its
# resample_hz / rolling_window_seconds drifted from the expected pair — a
# mismatch otherwise silently yields a model the Pi rejects. bindings.yaml +
# data/dbc/ ride along in git, so they are auto-consistent; do not pass a
# different --config.
#
# Everything is env-overridable:
#   PI_USER PI_HOST PI_SESSIONS PI_CONFIG PI_REPO
#   SESSIONS_ROOT LAPTOP_CONFIG BINDINGS IF_OUT LSTM_OUT TCN_OUT BEHAVIOR_OUT
#   DEVICE SEQUENCE_MODEL EXPECT_RESAMPLE_HZ EXPECT_ROLLING_WINDOW_SECONDS RSYNC_DELETE
#
# SEQUENCE_MODEL (lstm|tcn) overrides which sequence backbone retrains; default
# follows models.sequence_detector in the laptop config. Both lstm/tcn artifact
# dirs are pushed when present, so a backbone switch needs no push-side change.

PI_USER="${PI_USER:-canary}"
PI_HOST="${PI_HOST:-192.168.137.2}"
PI_SESSIONS="${PI_SESSIONS:-/data/canary/sessions/}"
PI_CONFIG="${PI_CONFIG:-/etc/canary/canary.yaml}"
# Pi-side canary repo clone that the daemon's ModelManager watches.
PI_REPO="${PI_REPO:-/home/canary/canary}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

SESSIONS_ROOT="${SESSIONS_ROOT:-./sessions}"
LAPTOP_CONFIG="${LAPTOP_CONFIG:-./canary.laptop.yaml}"
BINDINGS="${BINDINGS:-data/bindings.yaml}"
IF_OUT="${IF_OUT:-models/isolation_forest}"
LSTM_OUT="${LSTM_OUT:-models/lstm_autoencoder}"
TCN_OUT="${TCN_OUT:-models/tcn_autoencoder}"
SEQUENCE_MODEL="${SEQUENCE_MODEL:-}"   # empty = follow config; or lstm|tcn
BEHAVIOR_OUT="${BEHAVIOR_OUT:-data/behavior_models.yaml}"
DEVICE="${DEVICE:-cuda}"          # cuda=NVIDIA, cpu=fallback; mps is macOS-only
EXPECT_RESAMPLE_HZ="${EXPECT_RESAMPLE_HZ:-1}"
EXPECT_ROLLING_WINDOW_SECONDS="${EXPECT_ROLLING_WINDOW_SECONDS:-30}"

PY="${REPO_ROOT}/.venv/bin/python"
REMOTE="${PI_USER}@${PI_HOST}"
SSH_OPTS=(-o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)
RSYNC_OPTS=(-av --human-readable --partial)
[[ "${RSYNC_DELETE:-0}" == "1" ]] && RSYNC_OPTS+=(--delete)

step() { printf '\n=== %s ===\n' "$1"; }
die()  { printf 'error: %s\n' "$1" >&2; exit 1; }

check_ssh() {
  step "preflight: ssh ${REMOTE}"
  if ! ssh -o BatchMode=yes "${SSH_OPTS[@]}" "${REMOTE}" 'true' 2>/dev/null; then
    cat >&2 <<EOF
error: cannot authenticate to ${REMOTE} non-interactively.

Install this host's key once (you will be prompted for the Pi password):
  ssh-copy-id -i ~/.ssh/id_ed25519.pub ${REMOTE}

Then re-run this script.
EOF
    exit 1
  fi
  echo "ssh ok: $(ssh "${SSH_OPTS[@]}" "${REMOTE}" 'echo $(whoami)@$(hostname)')"
}

assert_preprocessing() {
  step "preprocessing parity guard (${LAPTOP_CONFIG})"
  [[ -s "${LAPTOP_CONFIG}" ]] || die "${LAPTOP_CONFIG} missing/empty — run 'pull' first."
  EXPECT_RESAMPLE_HZ="${EXPECT_RESAMPLE_HZ}" \
  EXPECT_ROLLING_WINDOW_SECONDS="${EXPECT_ROLLING_WINDOW_SECONDS}" \
  "${PY}" - "${LAPTOP_CONFIG}" <<'PY'
import os, sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
pp = cfg.get("preprocessing") or {}
got = (pp.get("resample_hz"), pp.get("rolling_window_seconds"))
want = (int(os.environ["EXPECT_RESAMPLE_HZ"]),
        int(os.environ["EXPECT_ROLLING_WINDOW_SECONDS"]))
if got != want:
    sys.exit(
        f"preprocessing mismatch: pulled config has resample_hz/"
        f"rolling_window_seconds={got}, expected {want}. Refusing to train — "
        f"this would produce a model the Pi rejects."
    )
print(f"ok: resample_hz={got[0]} rolling_window_seconds={got[1]}")
PY
}

do_pull() {
  check_ssh
  step "pull sessions  ${REMOTE}:${PI_SESSIONS} -> ${SESSIONS_ROOT}/"
  mkdir -p "${SESSIONS_ROOT}"
  rsync "${RSYNC_OPTS[@]}" -e "ssh ${SSH_OPTS[*]}" \
    "${REMOTE}:${PI_SESSIONS}" "${SESSIONS_ROOT}/"
  step "pull config    ${REMOTE}:${PI_CONFIG} -> ${LAPTOP_CONFIG}"
  scp "${SSH_OPTS[@]}" "${REMOTE}:${PI_CONFIG}" "${LAPTOP_CONFIG}"
  assert_preprocessing
}

do_train() {
  assert_preprocessing
  local rc_retrain=0 rc_refit=0
  step "retrain Isolation Forest + sequence detector (device=${DEVICE})"
  # `|| rc=$?` keeps `set -e` from aborting here: retrain exits non-zero only
  # when EVERY trainer failed; a partial run (e.g. IF ok, sequence failed) is
  # exit 0 by contract. The behavior refit below is independent of the anomaly
  # trainers and must run regardless, so it is never skipped by a retrain
  # failure. retrain trains IF + the configured sequence backbone (or the
  # SEQUENCE_MODEL override) into models/<model_type>.
  local seq_args=()
  [[ -n "${SEQUENCE_MODEL}" ]] && seq_args=(--sequence-model "${SEQUENCE_MODEL}")
  "${PY}" scripts/retrain.py \
    --sessions-root "${SESSIONS_ROOT}" --all \
    --config "${LAPTOP_CONFIG}" --bindings "${BINDINGS}" \
    --if-out "${IF_OUT}" "${seq_args[@]}" \
    --device "${DEVICE}" || rc_retrain=$?
  step "refit behavior models"
  "${PY}" scripts/refit_behavior_models.py \
    --sessions-root "${SESSIONS_ROOT}" --all \
    --config "${LAPTOP_CONFIG}" --bindings "${BINDINGS}" \
    --out "${BEHAVIOR_OUT}" || rc_refit=$?
  if [[ ${rc_retrain} -ne 0 || ${rc_refit} -ne 0 ]]; then
    echo "train issues — retrain rc=${rc_retrain}, refit rc=${rc_refit}; " \
         "not pushing broken artifacts" >&2
    return 1
  fi
}

do_push() {
  check_ssh
  step "push artifacts -> ${REMOTE}:${PI_REPO}"
  rsync "${RSYNC_OPTS[@]}" -e "ssh ${SSH_OPTS[*]}" \
    "${IF_OUT}/"   "${REMOTE}:${PI_REPO}/${IF_OUT}/"
  # Push whichever sequence backbone(s) were trained — only one is active, but
  # syncing both when present means a backbone switch needs no push-side change.
  for seq_out in "${LSTM_OUT}" "${TCN_OUT}"; do
    [[ -d "${seq_out}" ]] && rsync "${RSYNC_OPTS[@]}" -e "ssh ${SSH_OPTS[*]}" \
      "${seq_out}/" "${REMOTE}:${PI_REPO}/${seq_out}/"
  done
  rsync "${RSYNC_OPTS[@]}" -e "ssh ${SSH_OPTS[*]}" \
    "${BEHAVIOR_OUT}" "${REMOTE}:${PI_REPO}/${BEHAVIOR_OUT}"
  cat <<EOF

Pushed. No Pi restart needed — ModelManager polls ~30s and hot-swaps.
Watch:  curl -s http://${PI_HOST}:8000/status | jq '{active_model_version, emitter}'
        (expect active_model_version flipping non-null, emitter.observed climbing)
EOF
}

# Optional, NOT part of 'all'. The crashed-write chunks (zero-byte + truncated)
# are skipped by the loader so they are harmless to training; this just stops
# them cluttering every run's warnings. Dry-run unless CONFIRM=1. Only ever
# touches 0-byte files; the truncated chunk is reported, never auto-deleted.
do_clean_pi_junk() {
  check_ssh
  step "scan for zero-byte session chunks on ${REMOTE}:${PI_SESSIONS}"
  local found
  found="$(ssh "${SSH_OPTS[@]}" "${REMOTE}" \
    "find '${PI_SESSIONS}' -type f -size 0 -print" || true)"
  if [[ -z "${found}" ]]; then echo "none found."; return 0; fi
  echo "${found}"
  if [[ "${CONFIRM:-0}" != "1" ]]; then
    echo
    echo "DRY RUN. Re-run with CONFIRM=1 to delete the files listed above."
    echo "(The single truncated chunk is app-level; remove it by hand from the"
    echo " path the loader warns about — never auto-deleted.)"
    return 0
  fi
  echo "${found}" | ssh "${SSH_OPTS[@]}" "${REMOTE}" "xargs -r -d '\n' rm -v --"
}

case "${1:-all}" in
  pull)          do_pull ;;
  train)         do_train ;;
  push)          do_push ;;
  all)           do_pull; do_train; do_push ;;
  clean-pi-junk) do_clean_pi_junk ;;
  *) die "usage: $0 [pull|train|push|all|clean-pi-junk]" ;;
esac

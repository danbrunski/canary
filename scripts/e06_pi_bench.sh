#!/usr/bin/env bash
set -euo pipefail

# E06.3: build the canary_ecu_twin wheel on the Pi and run the paired
# Python/Rust benchmark (tools/bench_ecu_twin.py) -- without touching the
# daemon, its venv or its checkout.
#
# Run it ON the Pi, as `canary`, from a scratch copy of the tree (never from
# /home/canary/canary), e.g. from the laptop:
#
#   git archive HEAD rust src tests tools scripts data pyproject.toml \
#     | ssh canary@canary.local 'mkdir -p ~/e06-work && tar -xf - -C ~/e06-work'
#   ssh canary@canary.local 'bash ~/e06-work/scripts/e06_pi_bench.sh'
#
# What it refuses, and why:
# - a working copy that is the daemon's checkout (/home/canary/canary): the
#   build writes target/, wheels and a venv beside it;
# - any vehicle_state other than parked, bench or engine_off, and any active
#   capture: the benchmark saturates a core for minutes, and a run beside
#   collection measures neither side honestly ("No analysis on the Pi while it
#   collects", AGENTS.md). The state is read again afterwards; a run during
#   which it changed is marked invalid and exits 3.
#
# It never installs into the daemon's venv: the wheel is unpacked into a
# separate venv (bench-venv) that sees the daemon venv's packages through a
# .pth file (read-only), so the measured interpreter is the daemon's Python
# with the daemon's numpy/pandas. The build runs under `nice -n 19`; the
# benchmark itself runs at normal priority. Nothing is left running.
#
# Environment overrides: E06_WORK (default: this script's tree),
# E06_SESSION (a stored capture whose tuples are also run; default the
# October 4 ram-study capture when present), E06_DAEMON_VENV,
# E06_MATURIN, CANARY_STATUS_URL, CANARY_CONTROL_TOKEN_FILE, E06_BENCH_ARGS.

WORK="${E06_WORK:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DAEMON_CHECKOUT="/home/canary/canary"
DAEMON_VENV="${E06_DAEMON_VENV:-${DAEMON_CHECKOUT}/.venv}"
STATUS_URL="${CANARY_STATUS_URL:-http://localhost:8000/status}"
TOKEN_FILE="${CANARY_CONTROL_TOKEN_FILE:-/etc/canary/control.token}"
SESSION="${E06_SESSION:-/data/canary/sessions/nissan-xterra-2008/_capture-ram-study-2026-10-04T204557}"
CARGO_BIN="${HOME}/.cargo/bin"
MATURIN="${E06_MATURIN:-${HOME}/e06-build-venv/bin/maturin}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${WORK}/results"

if [[ "$(realpath "${WORK}")" == "$(realpath -m "${DAEMON_CHECKOUT}")" ]]; then
  echo "refused: ${WORK} is the daemon's checkout; run from a scratch copy (~/e06-work)" >&2
  exit 2
fi

# vehicle_state and capture, one line: `<state> <capture_active>`.
read_state() {
  local auth=()
  if [[ -r "${TOKEN_FILE}" ]]; then
    auth=(-H "Authorization: Bearer $(cat "${TOKEN_FILE}")")
  fi
  curl -fsS --max-time 5 "${auth[@]}" "${STATUS_URL}" | python3 -c '
import json, sys
status = json.load(sys.stdin)
state = (status.get("vehicle_state") or {}).get("state") or "unknown"
active = bool((status.get("capture_profile") or {}).get("active"))
print(state, "capture" if active else "no_capture")
'
}

# Prints the state on stdout (the facts on stderr); fails unless idle.
check_idle() {
  local when="$1" line state capture
  line="$(read_state)" || {
    echo "refused (${when}): cannot read ${STATUS_URL}" >&2
    return 1
  }
  read -r state capture <<<"${line}"
  echo "${when}: vehicle_state=${state} ${capture}" >&2
  case "${state}" in
    parked | bench | engine_off) ;;
    *)
      echo "refused (${when}): vehicle_state ${state}; run only parked, bench or engine_off" >&2
      return 1
      ;;
  esac
  if [[ "${capture}" == "capture" ]]; then
    echo "refused (${when}): a capture is active" >&2
    return 1
  fi
  echo "${state}"
}

before="$(check_idle before)" || exit 2
mkdir -p "${OUT}"

# -- build ---------------------------------------------------------------------------
export PATH="${CARGO_BIN}:${PATH}"
export CARGO_TARGET_DIR="${WORK}/rust/target"
unset RUSTFLAGS CARGO_ENCODED_RUSTFLAGS
rm -rf "${CARGO_TARGET_DIR}" "${WORK}/wheel"
(cd "${WORK}/rust" && cargo fetch --quiet)
start=$(date +%s.%N)
(cd "${WORK}/rust" && nice -n 19 "${MATURIN}" build --release -m ecu_twin_py/Cargo.toml \
  -o "${WORK}/wheel" 2>&1 | tail -3)
build_s=$(python3 -c "print(round($(date +%s.%N) - ${start}, 1))")
wheel="$(ls "${WORK}"/wheel/canary_ecu_twin-*.whl)"

pyo3="$(sed -n '/^name = "pyo3"$/{n;s/version = "\(.*\)"/\1/p}' "${WORK}/rust/Cargo.lock")"
python3 - "${OUT}/build-${STAMP}.json" <<EOF
import json, subprocess, sys
def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
json.dump({
    "rustc": run("${CARGO_BIN}/rustc", "-vV"),
    "cargo": run("${CARGO_BIN}/cargo", "-V"),
    "maturin": run("${MATURIN}", "-V"),
    "pyo3": "${pyo3}",
    "daemon_python": run("${DAEMON_VENV}/bin/python", "-VV"),
    "command": "nice -n 19 maturin build --release -m rust/ecu_twin_py/Cargo.toml (clean target dir)",
    "flags": {
        "profile": "release (workspace defaults: opt-level 3, no LTO, codegen-units 16, "
                   "overflow-checks off, debug-assertions off)",
        "RUSTFLAGS": None,
        "pyo3_features": ["extension-module", "abi3-py312"],
    },
    "build_seconds": ${build_s},
    "wheel": "$(basename "${wheel}")",
    "wheel_bytes": $(stat -c %s "${wheel}"),
    "unpacked_so_bytes": None,
}, open(sys.argv[1], "w"), indent=2)
EOF

# -- the measurement venv (never the daemon's) -----------------------------------------
BENCH_VENV="${WORK}/bench-venv"
rm -rf "${BENCH_VENV}"
"${DAEMON_VENV}/bin/python" -m venv --without-pip "${BENCH_VENV}"
site="$("${BENCH_VENV}/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
daemon_site="$("${DAEMON_VENV}/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
echo "${daemon_site}" >"${site}/daemon-venv-readonly.pth"
"${BENCH_VENV}/bin/python" -m zipfile -e "${wheel}" "${site}"
so="$(ls "${site}"/canary_ecu_twin/*.so)"
python3 - "${OUT}/build-${STAMP}.json" "$(stat -c %s "${so}")" <<'EOF'
import json, sys
data = json.load(open(sys.argv[1]))
data["unpacked_so_bytes"] = int(sys.argv[2])
json.dump(data, open(sys.argv[1], "w"), indent=2)
EOF

# -- the paired run --------------------------------------------------------------------
session_args=()
if [[ -d "${SESSION}" ]]; then
  session_args=(--session "${SESSION}")
fi
status=0
(cd "${WORK}" && PYTHONDONTWRITEBYTECODE=1 "${BENCH_VENV}/bin/python" -m tools.bench_ecu_twin \
  "${session_args[@]}" --build-info "${OUT}/build-${STAMP}.json" \
  --out "${OUT}/pi-${STAMP}.json" ${E06_BENCH_ARGS:-}) || status=$?

after="$(check_idle after)" || {
  echo "INVALID: the vehicle state left idle during the run; discard ${OUT}/pi-${STAMP}.json" >&2
  exit 3
}
if [[ "${after}" != "${before}" ]]; then
  echo "note: vehicle_state ${before} -> ${after} during the run (both idle)"
fi
echo "results: ${OUT}/pi-${STAMP}.json  build: ${OUT}/build-${STAMP}.json"
exit "${status}"

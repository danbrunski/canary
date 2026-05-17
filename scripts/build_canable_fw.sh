#!/usr/bin/env bash
set -euo pipefail

# Reproducibly build the CANable2 candleLight (gs_usb) firmware that the Pi
# deployment runs, and verify it byte-for-byte against the committed binary.
#
#   scripts/build_canable_fw.sh            # build + verify, leave .bin in workdir
#   scripts/build_canable_fw.sh --install  # also copy over firmware/<name>.bin
#
# Why this firmware at all: the stock can0.service / udev automation only works
# with a *native* can0 net device, which only gs_usb/candleLight firmware
# creates (slcan only exposes /dev/ttyACM0). See docs/canable-firmware.md for
# the full rationale, flash steps, and rollback.

REPO="https://github.com/normaldotcom/candleLight_fw.git"
# Pinned commit == the source behind canable.io's canable2_fw-ba6b1dd.bin.
COMMIT="ba6b1dd"
TARGET="canable2_fw"
EXPECT_SHA="0adcb0bf71b28089f652bf5be6ca6f8c466e1f9edba14cb6d29959b3793d70fb"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMMITTED="${HERE}/firmware/canable2_fw-${COMMIT}.bin"
WORK="${CANFW_WORKDIR:-/tmp/candleLight_fw}"

# --- toolchain check (do not auto-install; tell the user the one command) ---
missing=()
command -v arm-none-eabi-gcc >/dev/null || missing+=("gcc-arm-none-eabi")
command -v cmake >/dev/null || missing+=("cmake")
command -v git >/dev/null || missing+=("git")
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "error: missing build tools: ${missing[*]}" >&2
  echo "  install: sudo apt-get install -y gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi cmake git" >&2
  exit 1
fi

# --- fetch exact source ---
rm -rf "${WORK}"
git clone --quiet "${REPO}" "${WORK}"
git -C "${WORK}" checkout --quiet "${COMMIT}"
echo "source: $(git -C "${WORK}" rev-parse --short HEAD) ($(git -C "${WORK}" log -1 --format=%cd --date=short))"

# --- build ---
# `-fpermissive` is REQUIRED on GCC >= 14: this 2021 code has `return;` in
# non-void functions, which modern GCC promotes from warning to a hard error
# (-Wreturn-mismatch). It does not alter emitted code vs. the GCC-8 era the
# vendor built with — verified by the byte-identical sha256 check below.
mkdir -p "${WORK}/build"
cd "${WORK}/build"
export TOOLCHAIN_BIN_DIR=/usr/bin   # bypass the toolchain file's fragile autodetect
cmake .. \
  -DCMAKE_TOOLCHAIN_FILE=../cmake/gcc-arm-none-eabi-8-2019-q3-update.cmake \
  -DCMAKE_C_FLAGS="-fpermissive" >/dev/null
make "${TARGET}" >/dev/null

BUILT="${WORK}/build/${TARGET}.bin"
GOT_SHA="$(sha256sum "${BUILT}" | awk '{print $1}')"
echo "built : ${BUILT}"
echo "sha256: ${GOT_SHA}"

# --- verify reproducibility against the committed artifact ---
if [[ "${GOT_SHA}" != "${EXPECT_SHA}" ]]; then
  echo "error: sha256 MISMATCH" >&2
  echo "  expected ${EXPECT_SHA}" >&2
  echo "  got      ${GOT_SHA}" >&2
  echo "  (different toolchain version can change output; the committed binary" >&2
  echo "   is the reference. Do not flash an unverified build to vehicle hw.)" >&2
  exit 2
fi
echo "OK: reproduces firmware/canable2_fw-${COMMIT}.bin byte-for-byte"

if [[ "${1:-}" == "--install" ]]; then
  cp "${BUILT}" "${COMMITTED}"
  echo "installed -> ${COMMITTED}"
fi

echo
echo "Flash (device in DFU mode — hold BOOT, replug, lsusb shows 0483:df11):"
echo "  sudo dfu-util -d 0483:df11 -a 0 -s 0x08000000:leave -D ${COMMITTED}"

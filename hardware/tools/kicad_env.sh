# shellcheck shell=bash
# Sourced by the KiCad tool scripts: which KiCad runs, and how to hand it a path.
#   kicad_cli ARGS...  kicad-cli             kpath PATH   PATH as that KiCad opens it
#   kicad_py ARGS...   python with pcbnew    kicad_tmp    a scratch directory that KiCad can save into
#   kicad_quiet        filters KiCad's harmless stderr chatter (pipe through it)
# A native KiCad (kicad-cli on PATH and a python3 that imports pcbnew) is used when there is one.
# Otherwise, or when KICAD_BIN is set, it is a Windows install driven from WSL: KICAD_BIN is its bin
# directory (python.exe, kicad-cli.exe), handed Windows paths. Windows KiCad cannot save into the WSL
# filesystem: `pcb drc --save-board` failed there with "Cannot copy permissions" (2026-10-20), so
# kicad_tmp is under Windows %TEMP% in that case.

kicad_native_py=
if [[ -z ${KICAD_BIN:-} ]] && command -v kicad-cli >/dev/null; then
    for py in python3 /usr/bin/python3; do
        if "$py" -c 'import pcbnew' >/dev/null 2>&1; then
            kicad_native_py=$py
            break
        fi
    done
fi

if [[ -n $kicad_native_py ]]; then
    # kicad-cli resolves footprints through the global library tables, which a native install gets
    # only when the GUI first runs. Without them DRC reports a lib_footprint_issues warning per
    # footprint (123 on CANary-PWR) where it otherwise reports the real mismatches.
    kicad_tables=${XDG_CONFIG_HOME:-$HOME/.config}/kicad/$(kicad-cli version | cut -d. -f1,2)
    if [[ ! -e $kicad_tables/fp-lib-table ]]; then
        echo "kicad_env.sh: no global footprint table in $kicad_tables; copy KiCad's defaults:" \
            "cp /usr/share/kicad/template/{fp,sym}-lib-table $kicad_tables/" >&2
    fi
    kicad_cli() { kicad-cli "$@"; }
    kicad_py() { "$kicad_native_py" "$@"; }
    kpath() { printf '%s\n' "$1"; }
    kicad_tmp() { mktemp -d; }
else
    KICAD_BIN=${KICAD_BIN:-"/mnt/c/Program Files/KiCad/10.0/bin"}
    if [[ ! -e $KICAD_BIN/kicad-cli.exe ]]; then
        echo "no KiCad: install KiCad 10 (kicad-cli, and pcbnew for python3), or set KICAD_BIN" \
            "to a Windows install's bin directory" >&2
        exit 1
    fi
    kicad_cli() { "$KICAD_BIN/kicad-cli.exe" "$@"; }
    kicad_py() { "$KICAD_BIN/python.exe" "$@"; }
    kpath() { wslpath -w "$1"; }
    kicad_tmp() {
        local temp
        temp=$(wslpath "$(cd /mnt/c && cmd.exe /c 'echo %TEMP%' | tr -d '\r')") &&
            mktemp -d -p "$temp" canary-kicad.XXXXXX
    }
fi

kicad_quiet() { grep -v -E 'memory leak|image handler|property\.h\([0-9]+\): assert'; }

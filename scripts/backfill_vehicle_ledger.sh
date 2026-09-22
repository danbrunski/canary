#!/usr/bin/env bash
# One-time vehicle-ledger backfill for the dev vehicle (D03.26).
#
# Transcribes history that is already written down elsewhere in the repo into
# the ledger through scripts/vehicle_log.py add -- nothing here is recalled.
# Each entry's details name its source (the 2026-07-18 profile migration's only
# written record is D03.26 itself plus the operator memory file). Not transcribed, because no
# document records it: odometer readings, the cat-back exhaust install date,
# and which events should be corpus boundaries (all entries are recorded with
# corpus_boundary=false; correct one with the record_vehicle_event MCP tool's
# event_id if it should be a boundary).
#
# Dates are local (MDT) calendar dates; UTC times are given in the details
# where the source records one.
#
# The ledger is a per-host, gitignored file (daemon.vehicle_ledger, scoped by
# vehicle profile), so run this once on each host whose ledger should carry
# the history. Arguments pass through to vehicle_log.py; CANARY_PYTHON picks
# the interpreter (default .venv/bin/python). On the Pi, from the checkout:
#
#   scripts/backfill_vehicle_ledger.sh --config /etc/canary/canary.yaml
#
# Re-running is safe: an entry whose title is already in the ledger is skipped.
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${CANARY_PYTHON:-.venv/bin/python}"
ARGS=("$@")
existing="$("$PY" scripts/vehicle_log.py "${ARGS[@]}" list)"

add() {
  local occurred="$1" kind="$2" title="$3" details="$4"
  shift 4
  if grep -qF -- "  ${title}" <<<"$existing"; then
    echo "skip (already recorded): ${title}"
    return
  fi
  "$PY" scripts/vehicle_log.py "${ARGS[@]}" add \
    --occurred "$occurred" --kind "$kind" --title "$title" --details "$details" "$@"
}

LTFT=(--channel obd:LONG_FUEL_TRIM_1 --channel obd:LONG_FUEL_TRIM_2)

add 2026-06-28 mod "Shorty headers and intake manifold spacers installed" \
  "All four catalytic converters retained. Headers run without heat shields. Source: docs/roadmap/engine-tuning-support.md (vehicle mod state; G.2 dates the work 2026-06-28). The apparent trim step at this boundary was later shown to be an intake-temperature confound (same doc, 'The apparent step at the mod boundary is a temperature confound')."

add 2026-06-28 observation "Battery disconnect for header/spacer work cleared learned LTFT" \
  "LONG_FUEL_TRIM_1/2 read exactly 0.00 in every session from 2026-07-18 through 2026-09-10. Before the disconnect LTFT was active (May corpus: B1 +1.88 %, B2 +2.98 % mean). Source: docs/roadmap/engine-tuning-support.md G.2." \
  "${LTFT[@]}"

add 2026-07-18 observation "CANary data migrated to per-vehicle profile layout on the Pi" \
  "scripts/migrate_to_profiles.py applied on the Pi: existing /data/canary data nested under the nissan-xterra-2008 profile slug and profiles.yaml seeded. A storage-layout change, not a vehicle change. Source: D03.26 (docs/roadmap/tasks/D03-open-work.md) and the operator memory entry project_vehicle_profiles."

add 2026-07-26 mod "ECU flash: Osiris ZS44A initial calibration, K 1.07" \
  "Revision ZS44A-osiris-initial, calibration 1ZS44A00000, ROM 1ZS44A, flashed 2026-07-26T22:31Z (stock ZS04C through 16:21 MDT, Osiris from the 16:31 session). K 1.07 ran badly rich: the 7-29 log has the A/F sensors pegged at their 10.0 floor at 5000-5750 rpm, so AFR from that window is censored. Source: data/nissan-xterra-2008/tune_revisions.yaml."

add 2026-07-31 mod "ECU flash: ZS44A K 1.05, fuel-comp rpm axis rescaled" \
  "Revision ZS44A-k105. K reduced to 1.05, fuel-comp rpm axis rescaled 800-12750 -> 800-7200, max-load cells cut. Overcorrected: 11-13 % lean of target across 4000-6000 WOT on the 7-31 log. Exact flash time not recorded; the date is approximate. Source: data/nissan-xterra-2008/tune_revisions.yaml."

add 2026-08-03 mod "ECU flash: ZS44A K 1.06, smoothed AFR targets, added spark" \
  "Revision ZS44A-k106, ROM ZS44A-Tuned.OsirisROM (sha256 c5325776...8fa9). K 1.05 -> 1.06 plus smoothed AFR targets and added spark at 3000-4500 high load in one flash, which is why the spark edit was not separately measurable. Boundary 2026-08-03T18:45:21Z is the ROM file mtime; exact flash time not recorded, date operator-confirmed 2026-08-05. Source: data/nissan-xterra-2008/tune_revisions.yaml."

add 2026-09-20 mod "ECU flash: ZS44A single Fuel Targets Map 1 cell edit" \
  "Revision ZS44A-20260920, ROM ZS44A-Tuned-20260920.OsirisROM (sha256 03d00051...28a2), flashed 2026-09-21T03:08:45Z (evening of 2026-09-20 MDT). One cell at 5200 rpm / BFS 18.20: AFR 11.6148 -> 11.5436; K and all other bytes unchanged. The flash reset the learned LTFT table (0.00 % on both banks for the 2026-09-21 drive). Source: data/nissan-xterra-2008/tune_revisions.yaml, docs/skunkworks/uprev-flash-20260920.md, data/analyses/2026-09-21-drive-tuning.md." \
  "${LTFT[@]}"

add 2026-09-18 observation "LTFT resumed learning (first seen active 2026-09-18)" \
  "LONG_FUEL_TRIM_1/2 were 0.00 through 2026-09-10 and active in every session from 2026-09-18 on (84-100 % of samples nonzero, range -9.4..+9.4 %). It resumed somewhere in that eight-day window; what re-enabled it is not known, so 2026-09-18 is the first observation, not the event date. Source: docs/roadmap/engine-tuning-support.md G.2." \
  "${LTFT[@]}"

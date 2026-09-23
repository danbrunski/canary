# Next drive — deploy plan (written 2026-09-23 after the morning drive)

## Pi state (updated 2026-09-23 evening)

**Deployed, nothing pending.** canary-a2 deployed `0d4b32b` by bundle (GitHub pull
fails on the Pi again), restarted through `scripts/restart_canary.sh`. It carries
A01–A06, S01, D03.28 and D03.29. Healthy: both services active, memory limits
enforced, first record stored 5.08 s after process start. D03.29 changes trip end:
read its commit before trusting or doubting a digest. D05.1 (the 37 s DID start-up
gap) is canary-a2's next deploy. Check `git log 0d4b32b..main` at the first wake-up.

**D03.28 is half verified.** At idle, three `since`-bounded `/anomalies` calls took
0.048 s each, with 85/85 ticks. **Owed from this drive, with the engine running and
the 5 s poller on:** one `GET /anomalies?since=<now − 30 min>` mid-drive. It passes if
it answers in < 1 s and the poller shows no missed tick, no `/status` latency spike and
no storage-queue spike in the two polls around it. Then tick D03.28's last box. Never
call it without `since`.

## First wake-up checks added by this drive

- `/status` `gps.has_fix`. With no fix, trip end cannot fire and the digest will not
  run (D03.29): say so on the log's first line.
- LTFT both banks. It read exactly 0.00 % for the whole 09-23 drive, three days after
  the 09-20 flash. Any non-zero value is news; log when it first moves.

## The drive itself

A tuning run: `tuning` from the first `engine_running` to parking (or the autostart
from `cdd355e`, `POST /control/capture/autostart {"profile":"tuning","coolant_min_c":80}`).

### OUTCOME, in priority order

1. **NA1** — three steady 3rd-gear pulls, ≤ 2,500 → ≥ 5,500 rpm, same road, warm. Needs
   the operator before the drive. On 09-23 the only WOT run was a standing start through
   three gears; its 3rd-gear segment began at 3,890 rpm. To get a pull that starts low,
   the driver settles in 3rd at ~2,000–2,500 rpm (≈ 40–50 km/h on this truck) before
   going to the floor.
2. **WOT delivery, per pull** — the 09-23 morning pull read −0.8 % in 3rd gear, and the
   afternoon read +2.3..+4.1 % on four events (+3.6 % on a 2nd-gear pull), so the ~3 %
   lean still holds. Report each pull separately. Two or more pulls outside
   +1..+5 % is a finding under the frozen rule; the lean question itself stays closed
   without fuel pressure (NA2).
3. **Hygiene** — health line each wake-up; exactly one `since`-bounded `/anomalies` read
   for D03.28's engine-running check (above), and tail the JSONL over ssh otherwise.
4. **If the operator flashed N01.1 or N01.2 before this drive**
   ([`docs/roadmap/tasks/N01-na-rom-response-experiments.md`](../../docs/roadmap/tasks/N01-na-rom-response-experiments.md)):
   first confirm the flash has its own entry in `tune_revisions.yaml` with its flash
   time. `calibration_matches_registry` cannot show it: UpRev flashes on this truck keep
   calibration id `1ZS44A00000`, so an unrecorded flash still reads as a match. Then copy that item's predictions into the
   log, unchanged, before the capture starts, and measure them per cell against the
   2026-09-22/23 medians. That comparison outranks NA1 on a drive after a flash,
   because it needs no maneuver.

### Predictions (frozen; unchanged from 2026-09-22 except the idle STFT line)

- Known answers: RAM timing vs 0x0E median Δ 0°, 70–80 % within 1°; RAM φ vs 0x44 ratio
  1.000; RAM rpm vs PID rpm 1.000. A miss stops the capture.
- Delivery on 0x24, load ≥ 85 %, first second dropped: +2..+4 % lean, flat 2,500–5,500.
- WOT timing medians 8–14° BTDC, rising above 4,500.
- VE 0.85–0.91 over 3,000–5,000 rpm with measured baro and IAT.
- Warm STFT: cruise (15–70 g/s) negative on both banks. **Idle: no prediction.** It read
  negative on 09-22 and 09-23 AM and positive on 09-23 PM, all with LTFT at 0.00 %.
- **Pass for NA1:** ≥ 3 pulls, each continuous at load ≥ 85 % in 3rd from ≤ 2,500 to
  ≥ 5,500 rpm.

## Still open (do not re-derive)

- WOT ~3 % lean is closed to drives without fuel pressure (NA2).
- D03.29: trip end depends on GPS alone, and the digest picked the post-capture session
  on 09-23 PM. canary-a2 owns both.
- **D05.1 (new, 09-23 PM):** six of eight `tuning` DIDs were silent for 37.5 s after the
  capture started. At every `tuning` start, read each DID's first-row time from the
  capture and log it. A repeat gives the desk its reproduction.
- D03.18 note: a 2.5 s Mode-01-only gap with one timeout on 09-23; different from the
  fixed session-control path. Look at it only if it recurs.

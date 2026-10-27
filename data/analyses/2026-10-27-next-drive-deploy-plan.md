# Next drive after 2026-10-27: deploy plan

Written at the end of the 2026-10-27 drive (`2026-10-27-drive-log.md`). It
supersedes `2026-09-23-next-drive-deploy-plan.md`, which was stale.

## OUTCOME

Find which runtime gate keeps long-term fuel trim from learning, then see
whether learning absorbs the new pump's ~3 % extra flow. Learning that runs is
the cheapest tuning fix there is: the ECM corrects itself in every cell it
visits in closed loop, and that correction carries into open loop.

## Preconditions (each logged met / not met at the first wake-up)

1. **The fuel-pressure ADC and the NVMe both connected.** On 10-27 the NVMe
   had to come out to make room for the ADC, which killed the daemon (I/O error
   on `/data`). A short USB extension, or moving one to the other controller's
   port, needs to happen first. The ADC is found by port path
   (`platform-xhci-hcd.1-usb-0:2:1.0-port0`). Check `lsusb` for a CH340 and
   `sensor:FUEL_PRESSURE` in `/telemetry/latest` before leaving.
2. **GPS antenna where it sees sky.** On the dash it got a fix within
   2.5 min on 10-27. In its usual spot it saw 0 satellites from 10-10 to 10-27.
3. **`canary-mcp` running.** It was down from 16:06Z on 10-27.
4. **Starlink powered for the whole drive.** It dropped at key-off on 10-27, so
   the trip-end digest couldn't be checked. Keep it up ≥ 5 min after key-off,
   or plug in the Ethernet cable at the stop.

## Pending code (desk, before the drive; nothing is deployed yet)

- **LTFT gate RAM channels** in `data/dids.yaml` `memory:`, read-only 0x23 at
  slow weight, plus a capture profile carrying them on top of `tuning` (a new
  profile, not an edit to `tuning`):
  - per-bank gate bits 0xFFFF1518 / 0xFFFF151A, the condition 0xFFFF179C, and
    counters 0xFFFF138A, 0xFFFF1686, 0xFFFF1435–1436;
  - inputs 0xFFFF26BA (closed loop), 0xFFFF1726 (after-start enrichment),
    0xFFFF2834, 0xFFFF28B6–28B9, 0xFFFF2792, 0xFFFF358B–358E, 0xFFFF4857,
    0xFFFF284E, 0xFFFF2881;
  - **the EVAP purge controller's words** (Ghidra, 2026-10-27: it writes
    every path input of the gate): w5224 (4 bytes, accumulated purge vapour
    since power-up; 28B9.6 needs ≥ 0x302<<15), w38AA (purge flow), 0x28B9,
    0x2792 (purge state, must be < 2 off idle);
  - rear-O2 offsets 0xFFFF4CA4–4CA7, already catalogued as
    `mem:ECM_AF_LEARNED_OFFSET_B1/B2` and readable as DIDs 0x1249/0x124A.
    The ±250 window is already refuted by data, so these are context only;
  - one-shot reads at the start: the visit counts 0xFFFF52F5..0x5374 and the
    maps 0xFFFF5460..0x555F.

  Source: the 10-27 D03 trim note, and the firmware trace (`FUN_00058714`,
  `FUN_00057cf8`, `FUN_00057b04`). Verify each address against
  `~/ecu-re/out/full.asm` before cataloguing; a constant matching a constant
  isn't evidence.
- A daemon restart to load the new catalog entries, done in the driveway.
- Optional: put `ECM_EVAP_PURGE_CMD` on the trim cadence in `default`, which
  only carries it slowly today.

## Predictions (frozen when the profile lands, before its data)

- **G1:** with the engine warm (coolant > 69 °C), in closed loop and steady,
  0xFFFF179C is clear on every read, or 179C is set and 1518 never is. Either
  way, the first false input on the reads where 179C is clear names the gate.
  Pass: one input is false on ≥ 95 % of warm closed-loop reads, while every
  other input of its path is true on ≥ 50 %. Unresolved: two or more inputs
  share the blame.
- **G2:** the visit counts all read 0 (the learner hasn't run since the
  09-20 flash). Fail: any nonzero count.
- **G3 (purge):** on warm off-idle reads, 28B9.6 is clear or b2792 ≥ 2 on
  ≥ 95 % of reads. Pass names the purge controller as the closed gate. Fail:
  both read open while 0x1518 stays clear.

## Drive order

1. `tuning`-plus-gates capture from the first `engine_running` to parking
   (a tuning run is the whole drive).
2. No maneuvers needed. Warm idle, cruise at 1,700–3,200 rpm, and one steady
   3rd-gear pull from 2,500 rpm if the road allows. That pull would also be
   NA1 and NA2: fuel pressure through a pull, which says whether 10-27's
   larger droop at 60–100 g/s is real.

## Still owed from 10-27

- F0: vent the AEM sender to atmosphere and settle the analog offset.
- P3: an open-loop cold start with a target. The `default` profile has no RAM
  φ, and on 10-27 closed loop engaged within 75 s of a 30 °C start.
- The `acquisition_gap` clause on `_capture-tuning-2026-10-27T160919`
  (`ECM_TARGET_EQUIV_RATIO` five gaps of 2.5–6.5 s in its first 91 s, spark
  window one of 5 s at 214 s, zero losses). Check on the next capture whether
  it repeats outside the first 90 s after a daemon start.
- The trip-end digest for 10-27, unverified (the Pi was unreachable after
  key-off). Look for `digest.json` or `digest_skipped.json` in
  `2026-10-27T161320`.

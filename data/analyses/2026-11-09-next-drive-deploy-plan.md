# Next drive after 2026-11-08: deploy plan

Written at the end of the 2026-11-08 drive (`2026-11-08-drive-log.md`). It
supersedes `2026-10-27-next-drive-deploy-plan.md`. The fuelling reasoning is in
`2026-11-08-tuning-proposals.md`, and the LTFT gate in
`2026-11-08-ltft-learning.md`.

## OUTCOME

Work out what makes low load run lean, before any ROM edit is made. On 11-08,
purge-off warm idle ran +7 to +9 % total trim on bank 1 and +4 to +6 % on
bank 2. The error is shaped like 1/MAF, and it is new since 10-27. Bank 1 also
ran about 3 % leaner than bank 2 at every load. The next drive should
separate three causes:

1. **Leak or MAF low-flow error**: fixed extra air, so the error scales with
   1/airflow.
2. **Dead time**: a fixed pulse-width error, so it scales with 1/pulse.
3. **A bank-1 A/F sensor bias** (or a bank-1 injector): a constant percentage
   on one bank.

As a by-product, get one engine start long enough for the LTFT gate to open.

## Before the drive (desk and garage)

1. **Pull leg 3 first.** `_capture-ltft-gate-2026-11-08T201601` is on the Pi's
   NVMe, unpulled. It was still recording when the laptop lost the link at
   21:59:27Z.
   - Pull it with a tar stream, then check two things. A `capture_quality.json`
     means the capture was finalized. `digest.json` or `digest_skipped.json`
     (with its reason) says what the trip-end digest did.
   - Run `decompose.py`, `trim_map.py` and the LTFT tables from
     `~/canary-data/analysis-scripts/2026-11-08-drive/` on it.
   - It holds the day's longest single start: 20:37:50–21:58:52, with 22 min
     of steady cruise. The accumulator reached 728 of 770.
2. **Smoke-test the intake** after the MAF: boots, PCV and the plenum
   gaskets. Look at the bank-1 exhaust ahead of its A/F sensor. Neither needs
   a drive. Record any leak found and fixed as an intervention in
   `labelled_events.yaml` before driving.
3. **Mechanical gauge on the rail's Schrader valve**, read cold and again
   after the drive. On 11-08 the AEM read fell from 379 to 348 kPa over the
   afternoon. A gauge that does not fall means the AEM sensor drifts with
   heat. A gauge that falls the same way means the regulator or pump does.
4. **Optional, operator's call: swap the two A/F sensors** between banks.
   Record it in `labelled_events.yaml`, because after a swap "bank 1 sensor"
   names a different part.
5. **Arm the daemon's own capture, not a laptop poller.**
   `POST /control/capture/autostart` with
   `{"profile": "ltft-gate", "coolant_min_c": null}`.
   - The daemon starts the capture on the first `engine_running` tick and stops
     it 10 s after the engine stops. That rides through a 3 s stall and ends
     before parking, with or without the laptop.
   - The rule lives in memory: **re-arm after every daemon restart or
     deploy.**
   - On 11-08 the laptop poller auto-started only once, so the capture after
     the 20:15 key-off had to be started by hand. Leg 3 was left open when the
     link dropped 4 s after `parked`.
6. Fuel-pressure ADC connected and `sensor:FUEL_PRESSURE` live. GPS fix.
   Starlink up.

## The drive

- **Warm up, then in the driveway, in neutral (about 6 min): held-rpm steps.**
  Hold idle, 1,500 and 2,500 rpm for 60 s each, then repeat the set once.
  - This is the test that separates leak from dead time. Free-revving raises
    airflow about in step with rpm, while air per stroke, and with it the
    pulse, stays about the same or falls. A leak predicts the trim falls as
    airflow rises. A dead-time error predicts it stays put.
  - On the road the two are confounded: Ti tracks MAF at any one rpm.
- **NA4 load steps at warm idle:** headlights, blower on high and rear
  defrost, 30 s on and 30 s off, three times. This finishes NA4, and the
  Vb swing gives the dead-time slope.
- **One engine start of at least 60 min with no key-off and no stall,**
  including at least 35 min of steady cruise. A stall-restart resets the purge
  accumulator, so rock crawling goes at the end of the drive or on another
  day.
- **At least three warm idle stops of 2 min or more,** spread through the
  drive, for the purge-off idle trim and its drift over the drive.
- No pull is needed. NA1/NA2 wait for the KS1 harness repair.

## Predictions (frozen 2026-11-09, before any data)

Every trim is warm (ECT ≥ 80 °C), closed loop, purge off (DID 0x1110 = 0
within 0.6 s), and steady. Total trim is TT = (1+STFT)(1+LTFT)−1, paired per
sample. Each value is pressure-normalised with
TT + 100·(√((P+7.3)/(50.8+7.3)) − 1), P in psi from `sensor:FUEL_PRESSURE`.
The 11-08 references come from `decompose.py` on legs 1 and 2.

- **N1 — held-rpm steps (leak versus dead time).** Take the bank-mean TT at
  each held step and form the ratio R = TT(2,500) / TT(idle). Measure the
  airflow ratio A = MAF(idle) / MAF(2,500) and the pulse ratio
  T = (Ti−Ts)(idle) / (Ti−Ts)(2,500) from the same reads. Ti is DID 0x1206
  and Ts comes from Vb.
  - **Leak or MAF low-flow error:** R within ±30 % of A.
  - **Dead time:** R within ±30 % of T.
  - **Unresolved:** A and T differ by less than 30 % (the test cannot
    separate them), fewer than 20 purge-off reads at 2,500, or R near both
    or neither.
- **N2 — sensor swap, only if done.** At warm idle, purge off, the B1−B2 gap
  was +3.2 / +3.3 on 11-08.
  - **Sensor bias:** the gap flips to −3.2 ± 1.5.
  - **Bank hardware (injector or exhaust):** it stays at +3.2 ± 1.5.
  - **Unresolved:** it lands within ±1.0. The gap was within 1 % on every
    drive from 08-03 to 10-27, so it may have gone back on its own.
- **N3 — a leak found and fixed, only if so.** Purge-off warm idle TT falls
  into −4 to +2 % on both banks, which is the post-pump range on 10-26/27.
  The 0–15 g/s bands fall with it. The ≥ 25 g/s bands move by no more than
  ±1.5. **Falsifier:** idle TT is still above +5 % on bank 1.
- **N4 — LTFT gate.** In a single start, the accumulator (w5224>>15) reaches
  770, and 28B9.6 sets on or before that read. Within 15 min of closed-loop
  cruise after that, the visit counts (B1 0xFFFF5335, B2 0x52F5) rise.
  - **Falsifier:** 770 is crossed, 28B9.6 sets, and the counts don't move
    in 15 min of warm closed-loop cruise.
  - **Not tested:** no start lasts 60 min. That is a fact about the drive,
    not about the ECM.
- **No change made at all (control).** Purge-off warm idle reads +5 to
  +10 % on B1 and +2 to +7 % on B2. That re-measures the 11-08 state and
  gives its run-to-run spread.

## Ordering of changes

- One category of change per drive.
  - The leak check and its fix come first, because they need no drive.
  - The sensor swap can share a drive with a leak fix: N2 reads the bank
    difference and N3 reads the bank mean. The exception is a leak on one
    bank's runners, which would move both. Say so if that is what was found.
- **F1 (Fuel Comp +3 % over rows 0–4 × columns 0–5)** comes only if N1 says
  dead time or is unresolved, and no leak was found. It gets a drive of its
  own, with the predictions in `2026-11-08-tuning-proposals.md`.
  - A Fuel Comp edit on top of a leak would be wrong once the leak is fixed.
  - Flashing is the operator's: key on, engine off, from `ignition_only`,
    one explicit decision per flash. Record it in `tune_revisions.yaml`
    before driving.

## Open operator questions carried from 11-08

- Should the UPS critical-SoC floor override an armed keepalive? (drive log,
  "For the operator").
- D03.42: keep the four deferred phone-UI checks, or drop them?
- Rail pressure: the mechanical gauge reading (above).

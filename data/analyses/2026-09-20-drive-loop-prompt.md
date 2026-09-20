# /loop prompt for the 2026-09-20 CAN-only drive

Paste the block below. **Three lines are yours to settle first**, each marked
`<<`: the engine/boot state, whether the Pi takes HEAD, and D04.3. Everything
standing (capture budget, provenance, loss counters, trip-end check, criteria
hygiene) now lives in `.claude/commands/drive.md`, so it is not repeated here.

```
/loop /drive
OUTCOME: a five-hour CAN-only drive. In priority order:
1. Default-profile training coverage, plus live verification of the fixes
   deployed since 2026-09-18.
2. Wheel-speed agreement, then tagged decel-fuel-cut behaviour (D04.2).
3. Commanded-phi -> measured-lambda delay (D04.3), only if the deployed
   profile can resolve it.
4. Collection and trip-end behaviour over a long real drive.

DRIVE FACTS: pulls possible: yes · driving time: 5 hours ·
elevation change >= 800 m: maybe · UpRev along: no ·
engine at start: off, Pi booted first                                  << or: idling

PI CODE: deploy origin/main HEAD before the first capture and verify the
hash. It includes the mode-label fix f8b7d38, deployed ahead of the retrain
on purpose: at current polling rates it moves <= 0.1 % of live labels.
                                  << or: leave the Pi at 4ec2f4f0; f8b7d38 stays held

RESUME: data/analyses/2026-09-19-next-drive-deploy-plan.md and the latest
D03/D04 updates, but this priority order wins. That plan's UpRev tests do
not run today.

ALREADY ESTABLISHED — do not re-run as an outcome:
- 0x1206/0x1207 passed the injected-pulse test. Bank mapping, absolute ms
  scale and voltage-dependent dead time are unverified.
- 0x1134 is the active open-loop rich target, not every source of enrichment
  (after-start enrichment is separate).
- 0x1208 is air per event x tune K; raw/2040 ms is a BFS CANDIDATE. No UpRev
  today, so no BFS-scale confirmation. Never use 0x1208 as a fuel-cut
  indicator: it does not drop in fuel cut.
- Mid-rpm enrichment is a pedal switch (broadcast pedal >= ~227/255, ~89 %)
  into the fuel map's last load column. Load columns >= 0x6C are never
  reached at this altitude.

TARGETS:
- Wheel speed first, from default-profile data, no isolated capture: OBD
  SPEED vs broadcast wheel speed vs GPS in steady driving. Three instruments,
  so say which one disagrees; do not guess why.
- D04.2: >= 5 tagged in-gear coasts across two gears from ~3,000 rpm, plus one
  in neutral. The 0x1206/0x1207 floor marks the cut, corroborated by lambda
  and FUEL_STATUS. Report cut and refuel rpm, timing and hysteresis, with the
  acquisition resolution (0x1206 at 2 Hz is +-0.5 s). Take gear from the
  rpm/speed ratio once wheel speed is settled; speed-dependent conclusions
  stay unresolved if it is not.
- D04.3: only with a deployed profile that keeps 0x44 and 0x24 in separate
  mode01_batch_groups. Then >= 4 tagged tip-ins and matching lifts held
  >= 5 s, tip-in and lift reported separately with uncertainty. A tip-in must
  pass ~90 % pedal or it will not enrich below ~3,600 rpm.
                                  << delete this item if split-batch is not deployed
- D04.8: if one session spans >= 10 kPa of baro, tag and preserve it.

THIS DRIVE ONLY: I will say "parking in 10". If I don't, a refused digest
(a named digest_skipped.json) on the long final session is the expected
result, not a failure.
```

## Why it is shorter

The earlier draft ran ~90 lines; about half restated standing protocol. Those
rules moved into `drive.md` the same day so two copies cannot drift:
plan contents and frozen criteria, detector provenance before interpreting
silence, stop-when-the-target-is-met, prerequisites measured not configured,
instruments that disagree, coverage logging, close only exercised paths,
separate loss counters, and the trip-end check at the final stop.

## Before leaving

1. **Split-batch:** `mode01_batch_groups` is uncommitted work in the tree (not
   the drive agent's). D04.3 needs it committed, deployed, and a profile that
   puts 0x44 and 0x24 in different groups. Otherwise delete the D04.3 item.
2. **Boot order:** powering the Pi ~1 min before turning the key closes the
   key-off boot check that has been open since `63d5e51`, for free.
3. **Optional, decisive for BFS without UpRev:** a K-only reflash of 2–3 %.
   It predicts 0x1208 per gram of air moves by exactly the K ratio, resolvable
   in one drive at today's 0.3 % precision. If you do it, add the old and new
   K to DRIVE FACTS and a line under TARGETS.

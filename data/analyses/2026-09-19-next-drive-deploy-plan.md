# After the 2026-09-18 evening drive: the next drive

Written 2026-09-19 ~00:25Z. Evidence: `data/analyses/2026-09-18-eve-drive-log.md`.
Supersedes the fuel sections of `2026-09-18-pm-next-drive-deploy-plan.md`.

## Pending for the Pi

- **Deployed and verified:** `4ec2f4f0` (D03.18 fix, PID widths, renames +
  migration, capture-scoped batching, blindness window, width guard).
- **Committed, not yet deployed** (none needs the truck):
  - `32fa211` manifest check (test + tool only);
  - `9fd6032` candidate BFS tuning provider (analysis only; deploy with the next pull).
- **Held:** the mode-label fix (raw decoded counts vs engineering thresholds,
  since `998668a`). It changes live per-mode threshold selection, so it
  deploys **with** the IF/LSTM retrain. That waits on the operator's corpus
  choice (D02.5).
- **Owed on the Pi with the daemon stopped:**
  `tools/migrate_channel_names.py --config /etc/canary/canary.yaml --apply
  --daemon-stopped` over the sessions root, to migrate the `capture_profile.json`
  snapshots (refit eligibility keys on recipe names). Slow; do it at a desk.

## What is settled, and what isn't

| item | state |
|---|---|
| 0x1206/0x1207 = injected pulse | **passed** (matched air charge, +1.0/−0.5 %, n 184) |
| 0x1208 = air × tune K ≈ BFS (Tp) | **candidate**; BFS_ms ≈ raw/2040 ±1.5 %, derived era-wise |
| 0x1134 = active open-loop rich target | supported; its rename is done |
| WOT enrichment 1,600–3,600 rpm | pedal ≥ ~227/255 → fuel map's **last column** |
| D04.3 transport delay | blocked: 0x44 and 0x24 must be in separate batches |
| injector dead time vs voltage | unresolved: the alternator held the voltage |

## The next drive: UpRev along (decisive for BFS)

**Constraint (operator, 2026-09-19):** while UpRev logs, CANary must be
**passive only**: no OBD/UDS requests, so no 0x1208 polling. A same-instant
0x1208-vs-BFS pairing is therefore impossible. CANary still records the
broadcast bus passively (RPM 0x1F9, pedal, throttle, wheel speed).

**Method: a chain through air per event, in alternating windows at repeated
operating points.**
- **UpRev windows** (CANary passive with a TTL): BFS, MAF and RPM come in one
  UpRev frame, so r_U = BFS / (MAF·20/rpm) per row, same instant within UpRev.
- **CANary windows** (UpRev stopped): 0x1208 + Mode 01 MAF + RPM, so
  r_C = 0x1208 / (MAF·20/rpm) per pair, same instant within CANary (hybrid
  pairing ≤ 350 ms, stability-gated).
- **Constant = r_C / r_U, compared per matched cell**, never across regimes.
- **Known-answer gates:** broadcast RPM vs UpRev RPM 1:1 in UpRev windows
  (clock alignment, `align_clocks`); broadcast RPM vs Mode 01 RPM 1:1 in CANary
  windows. MAF is the same sensor on both sides (UpRev MAF vs Mode 01 MAF slope
  0.987 on 07-27). Refuse a cell whose gate fails.

### Predictions, written now
| claim | passes if | falsified if |
|---|---|---|
| 0x1208 = BFS × k | in every matched cell (warm idle, idle + A/C, 2,000 and 3,000 rpm steady, ≥ 2 plateaus above 9 ms BFS) with ≥ 30 rows per side, r_C / r_U = 2040 ± 2 % and the spread across cells ≤ 2 % | any cell off by > 5 %, or the idle → idle + A/C step in r_U and r_C differs by > 3 % (then 0x1208 isn't K·air in that regime) |
| pedal switch threshold | in an **UpRev window**, UpRev TARGET AFR switches to the last-column value between broadcast pedal 225 and 230 counts, on the way up and the way down (hysteresis reported) | no clean switch, or a switch below 220 counts |

### Drive order
1. **Pair A, warm idle:** 90 s UpRev window, then 90 s CANary window
   (`bfs-chain` profile: 0x1208, 0x1209, 0x1219 + Mode 01 MAF/RPM; build it at the desk).
2. **Pair B, idle + A/C:** the same alternation.
3. **Pairs C/D:** steady 2,000 and 3,000 rpm, 30 s per window, alternating.
4. **Pairs E/F:** part-throttle uphill plateaus above 9 ms BFS, alternating.
5. **UpRev window: the pedal staircase** at 2,000 and 3,000 rpm (215/222/225/227/229/232/240 counts, 5 s each, up then down), then one ≥ 30 s hold at pedal ≥ 235 (timer check).
6. Each switch: tag it; passive mode is armed with a TTL for every UpRev window and disarmed after.

## Desk before it
1. Operator: the retrain corpus (D02.5), then retrain IF + LSTM and deploy
   the mode-label fix with them. The manifest-check allowlist must then shrink.
2. A split-batch profile (0x44 and 0x24 never in one batch) for D04.3.
3. The `bfs-chain` profile above (0x1208/0x1209/0x1219 + Mode 01 MAF/RPM), with its rate measured; and a small script for r_C/r_U per matched cell with the gates.
4. Settle broadcast wheel speed vs GPS (8–16 % low) before D04.2 speed gating.

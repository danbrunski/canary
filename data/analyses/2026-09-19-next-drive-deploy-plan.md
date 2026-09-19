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

**Needs:** UpRev laptop logging B-FUEL SCHDL, MAF and RPM (K-line; it doesn't share
the CAN OBD channel, but arm CANary passive with a TTL for simultaneous
windows per memory). CANary on a minimal fast profile polling **0x1208,
0x1209, 0x1219** only (build it at the desk; measure its rate).

### Predictions, written now
| claim | passes if | falsified if |
|---|---|---|
| 0x1208 = BFS × 2040 | `compare_channels` raw 0x1208 vs UpRev BFS ms after an RPM 1:1 known-answer gate: slope 2040 ± 2 %, `stable`, a held-out plateau agrees | slope outside ±5 %, or the idle→idle+A/C step differs by > 3 % between the two |
| pedal switch threshold | a pedal staircase at 2,000 and 3,000 rpm (215/222/225/227/229/232/240 counts, 5 s each, up then down) switches 0x44 to the last-column value between 225 and 230 counts | no clean switch, or a switch below 220 |

### Drive order
1. Warm idle 1 min, then idle + A/C 1 min (the BFS discriminator).
2. Steady 2,000 and 3,000 rpm holds, 20 s each.
3. Plateaus above 9 ms BFS (part-throttle uphill in 3rd).
4. **Pedal staircase** at 2,000 and 3,000 rpm (above), on a `fuel-pw-research`
   capture so 0x44 is at 4 Hz.
5. One ≥ 30 s pedal ≥ 235 hold (timer check), one lift-off, one WOT pull.
6. Parked, if the voltage can be moved: an idle load step while the battery is
   charging after a long crank (dead-time voltage test).

## Desk before it
1. Operator: the retrain corpus (D02.5), then retrain IF + LSTM and deploy
   the mode-label fix with them. The manifest-check allowlist must then shrink.
2. A split-batch profile (0x44 and 0x24 never in one batch) for D04.3.
3. The minimal fast BFS profile above.
4. Settle broadcast wheel speed vs GPS (8–16 % low) before D04.2 speed gating.

# /loop prompt for the 2026-09-20 drive — the drive as a live test bench

The frame (operator, 2026-09-19): a drive is the period when the agent can
watch the collector under real load, change code, and re-verify within
minutes. So this prompt is built around work that only a loaded, moving rig
can do and that does not wait on the driver. `default` collection runs
underneath the whole time; the only maneuvers asked for are coasts.

**Lines marked `<<` are yours to settle before pasting.** Standing protocol is
in `.claude/commands/drive.md` and is not repeated here.

```
/loop /drive
OUTCOME: use this five-hour drive as a live test bench for the collector.
`default` collection is the background throughout. In priority order:
1. CAN-controller overflow (D02.2): measure it, then A/B the no-reboot
   candidate — staggering the scheduler's request rounds — in matched blocks.
2. Session rotation without a daemon restart, developed, deployed and verified
   live; then the trip-end digest run against a just-rotated session while the
   truck is still collecting.
3. A live acceptance run: the ground-truth known-answer checks executed on
   every closed block of this drive, on the laptop.
4. Driver-dependent, only when convenient: wheel-speed agreement, then tagged
   coasts for D04.2.

DRIVE FACTS: pulls possible: yes · driving time: 5 hours ·
elevation change >= 800 m: maybe · UpRev along: no ·
engine at start: off, Pi booted first                                  << or: idling

PI CODE: deploy origin/main HEAD before the first block and verify the hash.
It includes the mode-label fix f8b7d38, deployed ahead of the retrain on
purpose: at current polling rates it moves <= 0.1 % of live labels.
                                  << or: leave the Pi at 4ec2f4f0; f8b7d38 stays held

AUTHORISED THIS DRIVE (beyond drive.md's "small fixes only"):
- Build round-staggering behind a config flag that defaults OFF, deploy it,
  and A/B it. Landing it as the default stays my decision: report the trade.
- Build, deploy and verify a session-rotate control endpoint.
- Build the laptop-side acceptance tool. No Pi change needed.
NOT authorised from the road: any /boot/firmware edit or Pi reboot (SPI clock,
the phantom mcp2515-can1 overlay). A Pi that fails to boot loses the drive.
                                  << or: authorise one reboot at a named stop

RESUME: D02.2, D01.5, D03.6 and D03.11 in docs/roadmap/tasks/D03-open-work.md,
and data/analyses/2026-09-19-next-drive-deploy-plan.md for what is deployed.
That plan's UpRev tests do not run today.

1. OVERFLOW (D02.2). Do not merge it with socket loss (D01.5) or storage
   drops: three faults, three counters.
   - Baseline first: can0 rx_over_errors per minute on `default` and on
     `fuel-pw-research`, from counters scoped to each block.
   - A/B in matched blocks of >= 10 min, alternating A-B-A-B on comparable
     road and load, because D02.2's existing table is one window per row.
   - Report BOTH sides of the trade per block: overflows per minute, and
     intra-round acquisition skew / tuple skew / same-instant pair yield.
     A stagger that halves loss and breaks DID pairing is not a win.
   - Criteria, frozen now: "helps" = overflow rate lower in every B block than
     both neighbouring A blocks, by >= 2x in the median; "costs" = p95 tuple
     skew or pair yield worse by > 20 %. Anything else is unresolved.

2. ROTATION. A control endpoint that closes the current session and opens the
   next without restarting the daemon, keeping the capture window, the
   scheduler state and the sequence-model window alive.
   - Pass: zero dropped records and no all-channel gap >= 2 s across a rotate,
     the closed session pulls and loads, the window survives, and live
     "which session is current" readers follow (control.sessions.latest_session).
   - Then GET the digest for a just-rotated ~10-min session while collecting:
     report digest wall time, peak memory of its child, and the collector's
     drops and queue high-water during it. That is D03.6/D03.11 evidence a desk
     cannot produce. A small-session pass does not close long-session
     acceptance.

3. ACCEPTANCE. A laptop tool that runs the known-answer pairs on each closed
   block through compare_channels / read_channels: broadcast RPM vs Mode 01
   RPM, the MAF DID vs the MAF PID, OBD speed vs broadcast wheel speed vs GPS,
   baro vs GPS altitude, and segmenter labels from broadcast-only vs Mode 01.
   Expectations live as data beside tests/fixtures/ground_truth. A failing
   check becomes a task the same hour. Refusals (too few pairs, flat signal)
   are reported as refusals, not passes.

4. DRIVER ITEMS, no precise maneuvers:
   - Wheel speed is settled by item 3's three-way speed check. Say which
     instrument disagrees; do not guess why.
   - D04.2: when I say "coast", tag it. >= 5 in-gear coasts across two gears
     from ~3,000 rpm plus one in neutral is the target; fewer is reported as
     fewer. 0x1206/0x1207 floor marks the cut; never 0x1208. Timing resolution
     is +-0.5 s at 2 Hz. Gear comes from rpm/speed once speed is settled.
   - One key-off/key-on cycle at any stop exercises the silent-ECU paths and
     the D03.18 exit retry, which has never fired live.

ALREADY ESTABLISHED — do not re-run: 0x1206/0x1207 = injected pulse (passed);
0x1134 = active open-loop rich target; 0x1208 = air per event x tune K, raw/2040
ms a BFS CANDIDATE (no UpRev today, so no scale confirmation); mid-rpm
enrichment is a pedal switch (>= ~227/255) into the map's last load column.

THIS DRIVE ONLY: restarts cost corpus continuity and the sequence window, so
at most one per 30 min unless verifying a fix, and none during a block. I will
say "parking in 10"; if rotation has landed by then, use it instead of the
pre-parking restart.
```

## Before leaving

1. **Rank the two reboot candidates in the driveway, not on the road.** D02.2
   recorded 14 overflows in a 65 s idle window, so idle traffic is enough to
   compare `spimaxfrequency` 10 → 20 MHz and removing the phantom
   `mcp2515-can1` overlay, one at a time, each against the same window. A
   failed boot in the driveway costs five minutes; on the road it costs the
   drive.
2. **Boot order:** powering the Pi ~1 min before turning the key closes the
   key-off boot check open since `63d5e51`.
3. **Split-batch (`mode01_batch_groups`)** is still uncommitted work in the
   tree and is not the drive agent's. D04.3 is deliberately absent from this
   prompt; add it back only once that is committed and deployed.
4. **Optional, decisive for BFS without UpRev:** a K-only reflash of 2–3 %
   predicts 0x1208 per gram of air moves by exactly the K ratio. If you do it,
   add the old and new K to DRIVE FACTS.

## Not in this prompt, on purpose

- The laptop shadow pipeline for a retrained model. It needs your retrain
  corpus choice (D02.5) to mean anything; rotation (item 2) is its
  prerequisite, so it fits the drive after this one.
- Deliberate fault injection beyond the one key cycle. Worth a drive of its
  own once rotation makes recovery cheap to observe.

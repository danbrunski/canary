# In-Vehicle Capture Run-sheet

The exact, ordered procedure for the drive backlog. Doing these ~5 sessions
(~2 h total) unlocks Phase 2 (Isolation Forest) and the first behavior-surface
fit. The *why* and the full backlog are in `docs/roadmap/archive/data-collection-plan.md`;
this is the operational checklist.

**The one rule that matters:** capture with Mode 01 logging
(`--obd-source canable`, the default). Do **not** run a Mode 22 / `0xC0`
DID sweep before or during a drive — the Nissan ECM's `0xC0` session is
mutually exclusive with Mode 01, and a DID-only corpus is what blocked the
behavior-model refit (`data-collection-plan.md`, Status 2026-05-17).

## One-time, per Pi

```bash
# Resilient, auto-recovering can0 (survives reboot + USB re-enumeration):
sudo install -m0644 systemd/can0.service /etc/systemd/system/can0.service
sudo install -m0644 systemd/70-canable-can0.rules \
        /etc/udev/rules.d/70-canable-can0.rules
sudo udevadm control --reload && sudo systemctl daemon-reload
sudo systemctl enable --now can0.service
```

See `docs/can-setup.md` for the details and the `canN`-naming caveat.

## Every session

1. **Bring the link up** (no-op if `can0.service` already did it):

   ```bash
   sudo scripts/canup.sh
   ```

2. **Preflight — go/no-go.** Engine running, then:

   ```bash
   .venv/bin/python scripts/preflight.py --bring-up
   ```

   Proceed only on `GO`. A `NO-GO` prints the exact fix. The most common
   real failure is `obd_mode01` empty → the adapter is still in a `0xC0`
   DID session: power-cycle it and re-run.

3. **Record.** One terminal:

   ```bash
   .venv/bin/python scripts/record.py --obd-source canable \
       --vehicle-state <state>
   ```

   `<state>` ∈ `cold_start`, `engine_running_idle`, `driving`. Auto-reconnect
   is on by default — a mid-drive USB glitch rebuilds the bus into the *same*
   `frames.log` instead of ending the drive. Leave `--obd-pids` at `auto`
   (walks the ECU's supported set; portable).

4. **Tag it** (recommended for drives) — second terminal, while recording:

   ```bash
   .venv/bin/python scripts/guided_session.py --protocol drive
   ```

5. **Stop cleanly:** `Ctrl-C` once in the record terminal (or
   `scripts/stop_recording.py --session <dir>` from elsewhere). Wait for the
   finalized manifest line.

6. **Verify before moving on:**

   ```bash
   ls -t data/sessions | head -1                       # newest session dir
   python -c "import json,sys; m=json.load(open(sys.argv[1])); \
print(m['stopped_by'], m['frames_recorded'], 'reconnects=',m['reconnects'])" \
       data/sessions/<dir>/manifest.json
   head -1 data/sessions/<dir>/pids.jsonl              # Mode 01 present?
   ```

   Want: `stopped_by` ∈ `signal`/`deadline` (not `error` /
   `reconnect_exhausted`), non-zero `frames_recorded`, and `pids.jsonl`
   containing Mode 01 channels (RPM/SPEED/…), not only `DID_*`.

7. **Log it** in `data/sessions.md` (date, conditions, notes).

## The sessions (in priority order)

Run 1–5; 6–7 are opportunistic. From `data-collection-plan.md` §2.

| # | Session | `--vehicle-state` | Notes |
|---|---------|-------------------|-------|
| 1 | **Cold-start** — first start of the day, genuinely cold. Idle to operating temp (~5–10 min), then a short drive. | `cold_start` | Can't be faked; warmup curve + open→closed-loop. |
| 2 | **Warm idle baseline** — fully warm, idle 5–10 min. | `engine_running_idle` | Nominal-idle reference. |
| 3 | **City drive** — 20–40 min stop-and-go. | `driving` | Accel/decel transients, low-mod load. |
| 4 | **Highway drive** — 20–40 min ~65–75 mph + ramp accels. | `driving` | Steady high load + DFCO on coast-down. |
| 5 | **Load-sweep** — dwell at a grid of (RPM × load) points; a few WOT pulls; long DFCO coasts. | `driving` | The key one for surface fitting; coverage × dwell. |
| 6 | Off-road / 4-Lo (if accessible) | `driving` | Distinct mode; confirms 4WD bindings. |
| 7 | A/C on↔off pairs on the same segment | `driving` | High-load points; confirms A/C bindings. |

After session 5 you have enough for Phase 2 + a first behavior surface;
retrain with `scripts/train_anomaly_model.py` / `scripts/refit_behavior_models.py`
(see `docs/roadmap/archive/data-collection-plan.md` §3).

# Next drive — deploy plan (written 2026-09-22 after the afternoon drive)

## Pending for the Pi (deploy before the drive, not from the road)

`cdd355e` — capture-start fix, DID window re-plan, capture autostart. Deploy with
`sudo canary-update` and read its `source: <before> -> <after>` line against `cdd355e`.
Healthy within 3 min or revert. The Pi was at `a870887` at 17:53Z on 2026-09-22.

**Verify after deploy, in the driveway (idle bus is enough):**
1. `POST /control/captures {"protocol":"drive"}`, then `POST /control/capture
   {"profile":"tuning"}` → **200**, `superseded_capture_window.protocol == "drive"`.
   `DELETE /control/capture` → `resumed_capture_window` names the live session.
2. First Mode 22 row within ~6 s of that start (was 27.6 s). Read it from the capture,
   not from `/status`.

## The drive itself

**This is a tuning run: arm the autostart before leaving and let it run the drive.**

    POST /control/capture/autostart {"profile":"tuning","coolant_min_c":80}

Then confirm `GET /control/capture/autostart` reports `armed: true` and, once moving,
that `/status` `capture_profile.name` becomes `tuning` by itself. If the daemon has not
switched within a minute of the gauge reaching normal, fall back to starting the capture
by hand — do not spend the drive debugging it.

### OUTCOME, in priority order

1. **NA1** — three steady 3rd-gear pulls, ≤ 2,500 rpm to ≥ 5,500 rpm, same stretch of
   road, engine warm. This is the only item that needs the operator, and it is the whole
   point of the drive: the last three drives produced tip-ins and standing-start pulls,
   never a steady 3rd-gear pull. Nothing else on this list outranks it.
2. **S01.6 (finish)** — start the 5 s poller *before* the key turns, and report the
   `ignition_only` → `engine_running` edge. Separately, one cold power-on with the engine
   started at the same moment: first stored row per source, measured from power-on.
3. **Hygiene** — the health line every wake-up; watch the autostart's own transitions.

### Predictions (frozen now; do not move them after seeing data)

Unchanged from 2026-09-22, because none of them was tested:
- Known answers first: RAM timing vs PID 0x0E median Δ 0°, ~80 % within 1°; RAM φ vs PID
  0x44 ratio 1.000; RAM rpm vs PID rpm 1.000. A miss stops the capture, it is not interpreted.
- Delivery on PID 0x24, load ≥ 85 %, first second of each enrichment event dropped:
  +2 to +4 % lean, flat across 2,500–5,500 rpm. Outside +1..+5 % on ≥ 2 pulls is a
  finding; inside it, "reproduced".
- WOT timing medians 8–14° BTDC, rising above 4,500 rpm.
- VE 0.85–0.91 over 3,000–5,000 rpm with measured baro and IAT.
- **Pass for NA1:** ≥ 3 pulls, each continuous at load ≥ 85 % in 3rd gear from ≤ 2,500 to
  ≥ 5,500 rpm. Fewer is unresolved, with the count; tip-ins are reported as tip-ins.

## Still open (do not re-derive)

- **WOT ~3 % lean is closed to drives without fuel pressure** (NA2). Do not raise K, do
  not recommend a calibration change from a drive.
- **The injector-pulse gap** is window member rotation under the profile's own budget, not
  loss. If tighter same-instant guarantees are wanted, that is a `required_tuple` question
  at a desk, not a threshold change.

# Protocol: per-wheel speeds from the ABS module, and the unpinned DIDs

Two capture goals that share a vehicle session. Written 2026-08-07; nothing
here has been run yet, so treat every expectation below as a prediction to be
checked, not a result.

**Motivation.** The operator's Titan-swap suspension moves each wheel out ~3 in
(track ~1.60 m → ~1.75 m, +9.5%). In a corner the speed split across an axle is
`dv = omega * T`, so that split is now ~9.5% larger than a stock-geometry VDC
model expects for a given yaw rate — which reads as the outside wheels slipping.
SLIP/ABS activating in curves above ~30 mph is consistent with that. To say
anything stronger we need what the ABS module actually sees per corner, and we
currently cannot read it.

**What we already ruled out.** `0x6F7` bytes 0-1/2-3/4-5 are *not* FL/FR/RL.
Regressing their pairwise differences on GPS yaw rate over 703 cornering
samples fits a lateral separation of 0.001-0.012 m (r = 0.07-0.14) against
~0.8 m for a real half-track, and the A-C offset does not flip sign between
left and right turns. They are three vehicle-speed variants. See the 0x6F7
notes in `data/bindings.yaml`.

---

## Phase 0 — before the vehicle (deskside)

1. `git pull` on the Pi and restart the daemon. Several changes since the last
   deploy affect what gets recorded: the segmenter's broadcast fallbacks, the
   `persist` flag on the wheel-speed bindings, `SPEED` leaving the priority
   lane, and the capture-miss backoff fix.
2. Confirm `vehicle_state` reads sensibly before drawing any conclusion from a
   capture (`GET /status.vehicle_state`).

## Phase 1 — does any second module answer? (KOEO, stationary, ~5 min)

This is the gate. Every diagnostic exchange in every frame log this repo holds
is ECM-only: `0x7E0` request / `0x7E8` response. `0x7E2` **has never been
requested**, so "it never appears" is absence of evidence, not evidence of
absence.

`tools/scan_constant_dids.py` already supports this — `--header` is documented
as "7E0 = engine; trans 7E1, ABS 7E2, …". Read-only, service 0x22 only.

```bash
# Try the default session FIRST. The ECM's 0xC0 gate is an ECM quirk; most
# non-engine modules answer 0x22 in the default session, and if the ABS does,
# there is no Mode 01 blackout and this can run alongside normal telemetry.
.venv/bin/python tools/scan_constant_dids.py --canable --config /etc/canary/canary.yaml \
    --header 7E2 --start 0x0000 --end 0x0040 --passes 1 --out /tmp/probe-7e2.json
```

Sweep the addresses in this order, one short probe each:

| header | expect | note |
|---|---|---|
| `7E1` | nothing | Manual transmission, no TCM. **Negative control** — if this "answers", the probe is wrong, not the truck. |
| `7E2` | ABS/VDC | primary target |
| `7E3`–`7E7` | unknown | cheap to try while set up |
| `740`–`74F` | unknown | Nissan-specific range; only if the standard range is silent |

If a header answers, re-run with `--session C0` **and** without, and record
which is required. If nothing answers anywhere, stop: per-wheel speeds are not
reachable by Mode 22 on this vehicle, and the fallback is the broadcast
candidates in Phase 4.

## Phase 2 — DID sweep on whatever answered (KOEO → idle, stationary)

```bash
.venv/bin/python tools/scan_constant_dids.py --canable --config /etc/canary/canary.yaml \
    --header 7E2 --start 0x0000 --end 0x2000 --passes 2 --pause-prompt \
    --out /data/canary/re/abs-sweep-$(date -u +%Y%m%dT%H%M%SZ).json
```

`--pause-prompt` waits between passes so the physical state can change. Pass 1
key-on/engine-off, pass 2 idling. This separates constants from live signals
before any driving.

## Phase 3 — the discriminating drive

**This is the phase that identifies corners, and the only one that needs
motion.** Tag *physical actions*, not intentions — the analysis keys off the
tags, and a graph selection is not a tag.

Arm the all-DID capture, then tag each state as you enter it:

| tag | physical action | what it separates |
|---|---|---|
| `stationary_running` | parked, engine on, 30 s | all wheels read 0 — finds the zero point |
| `straight_steady` | straight road, steady ~50 km/h, 60 s | all wheels equal — finds the scale |
| `turn_left_steady` | **steady-radius left circle**, constant speed, 3+ full circles | right wheels faster |
| `turn_right_steady` | **steady-radius right circle**, same speed/radius | left wheels faster |
| `slip_event` | the curve where SLIP actually triggers | what the module saw when it fired |

The two steady circles are the whole point: a signal that is a left wheel
speeds up in one and slows in the other. A vehicle-speed signal does neither.
Sign flip is the discriminator, magnitude is the confirmation.

Do the circles in a large empty lot at low speed. The `slip_event` tag is the
only item that needs a real road at real speed — treat it as optional, and if
a passenger can drive or operate the laptop, do that.

## Phase 4 — analysis (deskside, no vehicle)

For every candidate DID and every unbound byte slice, fit

```
value ~ a * gps_speed + b * yaw_rate
```

and rank on the **yaw** coefficient, not on correlation with speed — a
vehicle-speed signal has `b = 0` by construction, which is exactly why
ranking on speed correlation found only more copies of vehicle speed.

Acceptance for "this is corner X":

- `b` flips sign between `turn_left_steady` and `turn_right_steady`. Non-negotiable.
- `|b|` implies a half-track near **0.875 m** post-swap (0.80 m stock).
- reads 0 during `stationary_running`.
- `a` implies a plausible km/h-per-count scale.
- pairs into two same-sign groups of two (left pair, right pair).

Then bind it, and only then compare against `obd:SPEED` with
`src.analysis.compare.compare_channels` for the scale.

**Do not pin the scale against `obd:SPEED` and call it ground truth.** That PID
is 8.2% under true ground speed on this vehicle, and the existing `0x6F7`
scale inherited exactly that error by being pinned against it. Use `gps:speed`
for any absolute claim.

Candidates already worth testing first, from a yaw sweep of the 2026-08-06
drive — **suggestive only, all same-sign so at most one side**:
`0x284[6:8]` (+0.81 m), `0x385[4:6]` (+0.86 m), `0x285[6:8]` (+1.04 m).

## Phase 5 — the seven unpinned DID scales

Separate goal, same drive. These decode to raw counts today because
`scale`/`offset` are `null`, which is correct behaviour — an unpinned scale
must keep yielding counts with `unit: "counts"` rather than an invented
engineering value.

| DID | name | what would pin it |
|---|---|---|
| `0x1208` | `ECM_INJECTOR_PW_B1` | **Highest value.** At steady state fuel mass flow = `MAF / AFR`, and also `PW x injectors x rpm/2 x flow_rate`. Simultaneous Mode 01 `MAF` + measured AFR pins the ms scale. Needs the `tuning-hybrid-research` profile (Mode 01 + Mode 22 together). |
| `0x1209` | `ECM_BASE_FUEL_SCHEDULE_CANDIDATE` | The `fuel-did-research` profile exists for exactly this. Still a candidate, not a provider. |
| `0x1110` | `ECM_INTAKE_VVT_SOLENOID_DUTY` | Physically bounded 0-100%. Look for saturation at both rails across a full cam sweep; the rails pin the scale without a reference. |
| `0x1139` | `..._DUTY_CANDIDATE_B2` | Same, plus agreement with 0x1110 on a bank-symmetric manoeuvre. |
| `0x1206` | `ECM_INTAKE_PRESSURE_1206` | Cross-reference `obd:INTAKE_PRESSURE` / `obd:BAROMETRIC_PRESSURE`. Remember the ~80 kPa operating altitude — never assume sea level. |
| `0x1207` | `ECM_INTAKE_PRESSURE_1207` | As above; also establish how it differs from 0x1206. |
| `0x1201` | `ECM_RPM_LOAD_CORRELATED_1201` | Against `obd:ENGINE_LOAD`, but **not** by gridding on load — it is BFS-derived. Grid on `rpm x MAF`. |

The `tuning-hybrid-research` capture profile (Mode 01 at 4 Hz alongside
Mode 22, using the confirmed `10 81` fast session exit) is the right tool for
most of these, because they need a simultaneous Mode 01 reference. Its own
`capacity_evidence` says to validate it on one short drive before trusting
tuning conclusions from it — do that first, on this drive.

## What this cannot answer

Whether the ABS module's internal circumference or track-width constants can
be *changed*. Nothing here writes: the OBD client's allowlist is Modes
01/02/03/06/07/09/0A/22, enforced at `CanObdClient.query`, and the write
services (0x2E, 0x2F, 0x31) are outside it by design. This protocol
establishes what the module measures, which is the prerequisite for deciding
whether the SLIP behaviour is geometry or a fault — a tone ring disturbed
during the swap presents similarly and is a mechanical fix.

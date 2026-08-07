# Protocol: per-wheel speeds from the ABS module, and the unpinned DIDs

Two capture goals that share a vehicle session. Written 2026-08-07 and **run
the same afternoon** — read "Results" first. Phase 1 returned a different
answer than the plan assumed, and most of what follows is superseded.

---

## Results, 2026-08-07 (supersedes Phases 1-3 below)

### The ABS does not answer diagnostics on our bus, and does not need to

Probed `0x7E1`-`0x7E7` and `0x740`-`0x77F` with Mode 22 while sniffing raw CAN.
Only `0x7E8` ever replied — the ECM. The probe was proven live first: Mode 01
PID 0x0D at `0x7E0` answered, and the sniff caught the daemon's own `10 C0` /
`10 81` session traffic, so the silence is real. One frame at `0x765`
(`03 7F 22 11`, serviceNotSupported) appeared once and never reproduced across
two further isolated attempts; treat it as unexplained, not as a module.

**But the wheel speeds are on the bus already.** With a Z1 scan tool on an
OBD-II Y-cable displaying live per-wheel speeds — confirmed updating as the
truck was rolled — a 25 s sniff caught *zero* frames from it. The ABS
live-data screen is reading passive broadcast traffic, not polling. So the
data is in every session we have ever recorded, and this is a decode problem.

The Z1 *is* on our CAN pair for generic OBD-II: on its OBDII menu it emits
`020100ffffffffff` on `0x7DF`, distinguishable from our daemon by 0xFF padding
where we pad 0x00. So both testers share pins 6/14, and the ABS screen simply
transmits nothing.

### Two testers cannot both poll this ECM

While the Z1 polled generic OBD-II, `0x7E8` responses to canary fell to
**zero** and the daemon's request rate backed off from 6.84 to 2.88 Hz. The
ECM serves one tester session; the second arrival wins and canary silently
loses all Mode 01. Recovery is automatic once the other tool stops.

This produced a real defect, now fixed (`src/dashboard/status.py`):
`vehicle_state` reported `engine_off` — "plugged in, key out" — with the
engine idling at 781 rpm, because a 27 s stale OBD rpm of 750 suppressed the
broadcast fallback. **If you run another scan tool alongside canary, expect
Mode 01 to stop.** Broadcast decode is unaffected.

### What `0x6F7` A/B/C actually are

Rolling the truck slowly back and forth (three episodes, 0.7-2.3 s each) with
the engine idling:

| field | bytes | behaviour |
|---|---|---|
| A | 0-1 | **exactly 0 throughout**, 0% nonzero |
| B | 2-3 | tracks the roll, peak 59 counts = 3.69 km/h |
| C | 4-5 | tracks the roll, within 1 count of B |
| D | 6-7 | constant at 3 — a counter or status, not a speed |

The Z1 displayed ~2 km/h per wheel over the same rolls, consistent with B/C at
3.69 km/h given display rounding and that it peaked briefly.

A is the surprise: over a full 63-minute drive it correlates with speed at
r = 0.99, yet it sits at exactly zero below ~4 km/h. **Whatever A is, it is
unusable at walking pace** — relevant to anything downstream treating it as
vehicle speed at low speed.

B and C tracking to within 1 count is *expected* here and proves nothing about
sides: rolling straight turns all four wheels at the same rate. The corner
question was already settled from real driving (see below).

### Per-corner speeds are still not identified

`0x6F7` A/B/C are not FL/FR/RL. Over 703 cornering samples from real driving,
regressing pairwise differences on GPS yaw rate fits a lateral separation of
0.001-0.012 m (r = 0.07-0.14) against ~0.875 m for a real half-track, and the
A-C offset is -0.44 km/h in left turns and -0.45 in right — a constant bias
where opposite corners must flip sign. A full-lock roll would only have made
this more dramatic; it would not have changed the verdict.

Seventeen byte-aligned slices across `0x6F7`, `0x284`, `0x285`, `0x280`,
`0x354`, `0x590` and `0x2D1` pass "zero at rest, linear in GPS speed, stable
scale", and every one lands within 0.08-0.15 m of vehicle speed. All vehicle
speed at differing resolutions.

### The blind spots that remain — where to look next

The sweeps above were byte-aligned and absolute. Neither covers:

1. **Bit-packed fields.** ABS frames commonly pack four 12- or 14-bit wheel
   speeds into 6-7 bytes; a byte-boundary sweep cannot see them. `0x284`,
   `0x285`, `0x280`, `0x29E` and `0x2A0` all run 49 Hz with 8 bytes — the right
   shape. A first bit-level pass over the roll window flagged `0x6F7`
   dominantly, but a sliding bit window produces many overlapping views of the
   same data and the hits were not deduplicated to aligned fields.
2. **Delta encodings.** If a wheel speed is transmitted as a small signed
   offset from vehicle speed, it sits near zero at *all* speeds and fails the
   "proportional to GPS velocity" test by construction — the criterion would
   reject exactly what we are hunting. `0x284`/`0x285` are nonzero at rest and
   were filtered out on that basis, which is the shape a delta encoding has.
3. Ids with <5000 frames (`0x004`, `0x342`, `0x512`, `0x682`) and fields wider
   than 2 bytes.

Both real gaps close on stored data with no vehicle time.

### Tooling defect found

`POST /control/obd_request` reports `responded: false` identically for a
negative response and for silence — proven by pointing it at the ECM with DID
`0x1101`, which is known to answer `7F 22 80`, and getting the same empty
payload as a dead address. That is why Phase 1 had to fall back to sniffing
raw CAN. Until fixed, **do not read a bare `responded: false` as "nothing is
there"**; confirm with `POST /control/can_activity`.

---

## Original plan (2026-08-07, pre-run)

Retained for the Phase 4/5 method and the DID work, which stand. Phases 1-3
are superseded by the results above.

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

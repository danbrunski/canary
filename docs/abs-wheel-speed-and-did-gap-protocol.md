# Protocol: per-wheel speeds from the ABS module, and the unpinned DIDs

Two capture goals that share a vehicle session. Written 2026-08-07 and **run
the same afternoon**. The results were corrected after checking the factory
network and brake-control wiring: the ABS uses both the vehicle CAN bus and a
separate DDL2 K-line diagnostic connection. Read "Results" first; the original
CAN-address sweep below is historical and is not an exhaustive module-discovery
procedure.

---

## Results, 2026-08-07 (supersedes Phases 1-4 below)

### Factory topology: ABS on CAN, with a separate K-line diagnostic path

The factory manuals remove the ambiguity left by the capture:

- `xterra_fsm/lan.pdf` LAN-54 and LAN-62 connect DLC pins 6/14 to the ABS
  actuator's CAN terminals 12/13. This is the same CAN pair CANary records.
- `xterra_fsm/gi.pdf` GI-46/47 and `xterra_fsm/brc.pdf` BRC-88 connect DLC pin
  7 to ABS terminal 10. That is the DDL2 K-line diagnostic path.
- `xterra_fsm/lan.pdf` LAN-6 explicitly distinguishes DDL2 units, which use
  K-line for diagnosis and CAN-H/CAN-L for control, from "Diag on CAN" units.
  CONSULT-III is a multi-transport tool; its name does not imply that every
  module diagnostic session is carried over CAN.
- `xterra_fsm/brc.pdf` BRC-29/30 and BRC-83 list `FR RH SENSOR`, `FR LH
  SENSOR`, `RR RH SENSOR`, and `RR LH SENSOR` as ABS data-monitor items. The
  CAN signal chart at LAN-46-48 lists the ABS-transmitted **vehicle speed**
  signal, but not four transmitted corner speeds.

The supported conclusion is therefore: the ABS is a normal participant on
pins 6/14 CAN and has a dedicated diagnostic path on pin 7 K-line. The factory
manual strongly favors K-line for the CONSULT ABS data monitor, but it does
not prove how the aftermarket Z1 implements its screen. No second CAN pair or
"wrong CAN pins" hypothesis is needed.

### What the CAN probes did—and did not—establish

Probed `0x7E1`-`0x7E7` and `0x740`-`0x77F` with Mode 22 while sniffing raw CAN.
Only `0x7E8` ever replied — the ECM. The probe was proven live first: Mode 01
PID 0x0D at `0x7E0` answered, and the sniff caught the daemon's own `10 C0` /
`10 81` session traffic, so the silence is real. One frame at `0x765`
(`03 7F 22 11`, serviceNotSupported) appeared once and never reproduced across
two further isolated attempts; treat it as unexplained, not as a module.

That rules out UDS/Mode-22 responses at those guessed CAN request IDs. It does
**not** prove that only the ECM supports CAN diagnostics: another module could
use different Nissan addressing or a diagnostic service other than Mode 22.
More importantly for ABS, the factory wiring identifies K-line as its
diagnostic transport, so a CAN Mode-22 header sweep was never a complete ABS
access test.

With a Z1 scan tool displaying live per-wheel speeds—confirmed updating as the
truck was rolled—a passive CAN capture caught *zero* frames transmitted by the
tool during the ABS screen. That test is complete. It leaves exactly two
explanations: the Z1 polled the ABS over pin 7, outside CANary's CAN
transceiver, or it decoded wheel speeds from already-present passive CAN
frames. The factory DDL2 wiring and the FSM's omission of per-corner speeds
from the CAN signal chart favor K-line, but the CAN capture alone cannot choose
between them.

The Z1 *is* on our CAN pair for generic OBD-II: on its OBDII menu it emits
`020100ffffffffff` on `0x7DF`, distinguishable from our daemon by 0xFF padding
where we pad 0x00. The same physical scan tool can use CAN in its generic OBD
menu while using either of the two non-transmitting strategies above in its
Nissan ABS menu.

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

The Z1 displayed about 2 km/h per wheel while B/C reached a brief 3.69 km/h
peak. Those were not paired same-instant samples, so they establish only that
both instruments detected motion; they are not a scale or identity
cross-reference.

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
scale", and every one lands within 0.08-0.15 m of aggregate vehicle speed.
None has the yaw sign or magnitude required of an individual corner.

### Passive-decode blind spots that remain

The sweeps above were byte-aligned and absolute. They do not cover:

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

These are legitimate stored-data checks. The factory signal chart points to
aggregate speed being the documented CAN broadcast and the four internal
corner values being diagnostic data-monitor items, so K-line is the leading
hypothesis. But completing the bit/delta sweep is also how the passive-CAN
hypothesis can be tested from existing captures. A negative sweep would
strengthen the K-line conclusion; it would not prove that arbitrary hidden
encodings cannot exist.

### Decisive transport check and next collection step

The passive-CAN half of the transport test is already complete: CANary sent no
OBD requests, and the Z1 sent no CAN frames while its ABS wheel-speed values
updated. Do not repeat it. The remaining discriminating observation is pin 7:

1. Monitor DLC pin 7 with a high-impedance oscilloscope or automotive K-line
   receiver while opening the Z1 ABS wheel-speed screen. Do not connect a
   3.3 V GPIO directly to the nominal 12 V K-line.
2. Pin-7 request/response bursts would establish K-line. No pin-7 activity
   would leave passive CAN decoding as the explanation and make the bit/delta
   search the primary next step.

If pin-7 activity is confirmed, the useful engineering target is a passive or
read-only DDL2/K-line capture path and protocol decode. If pin 7 is quiet, the
stored passive-CAN search takes priority. Continuing to guess UDS CAN headers
is lower value in either case.

### Tooling defect found

`POST /control/obd_request` reports `responded: false` identically for a
negative response and for silence — proven by pointing it at the ECM with DID
`0x1101`, which is known to answer `7F 22 80`, and getting the same empty
payload as a dead address. That is why Phase 1 had to fall back to sniffing
raw CAN. Until fixed, **do not read a bare `responded: false` as "nothing is
there"**; confirm with `POST /control/can_activity`.

---

## Original plan (2026-08-07, pre-run; historical)

Retained as an audit trail and for the separate Phase 5 ECM-DID work. Phases
1-4 are superseded by the results above; do not execute their ABS steps as a
current runbook.

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

This was the original gate. Every diagnostic exchange then present in the
frame logs was ECM-only: `0x7E0` request / `0x7E8` response. The plan assumed
conventional ISO-TP physical addresses for the other modules; the factory
manual later showed that ABS diagnosis is DDL2 K-line instead.

`tools/scan_constant_dids.py` already supports this — `--header` is documented
with the historical guess "7E0 = engine; trans 7E1, ABS 7E2, …". That mapping
is not an Xterra FSM fact. The tool is read-only and service 0x22 only.

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
which is required. If nothing answers, the only valid conclusion is that
Mode 22 did not answer at the tested CAN addresses. It says nothing about
K-line, other CAN addressing, or other diagnostic services.

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

The original candidates from this method—`0x284[6:8]`, `0x385[4:6]`, and
`0x285[6:8]`—are **retracted**. The sweep discarded stationary samples and
divided a small yaw response by a near-zero speed coefficient, inflating noise
into plausible half-track values. Full-range testing showed that the 0x284
and 0x285 slices are not speeds. Do not re-propose them.

### The hybrid drive, 2026-08-07 17:09-17:35 UTC (Phase 5, first results)

`_capture-tuning-hybrid-research-2026-08-07T170918` — 26 minutes, 625-5588 rpm,
to 122 km/h. This is the "validate on one short drive" run the profile's own
`capacity_evidence` asks for. It validates, with caveats.

**The profile does deliver simultaneity.** 1024 one-second cells held all seven
of `did:ECM_INJECTOR_PW_B1`, `obd:MAF`, `obd:RPM`, `obd:COMMANDED_EQUIV_RATIO`,
`obd:FUEL_STATUS`, `did:ECM_BASE_FUEL_SCHEDULE_CANDIDATE` and `obd:ENGINE_LOAD`
with **zero staleness**. Ground truth passes: `decoded:ECM_RPM` vs `obd:RPM` is
slope 0.9988, r² 0.9955, n=1333, verdict `stable`.

**But Mode 01 and Mode 22 are interleaved, never concurrent.** They are mutually
exclusive on this ECM, so "simultaneous" means "within the same 1 s cell", not
"at the same instant". `did:ECM_RPM` vs `obd:RPM` reads slope 0.9939 / verdict
`drifting` purely from that offset. **Restrict to steady state before drawing a
quantitative conclusion** — this is not optional, it is the difference between
r² 0.79 and r² 0.96 on the injector fit below.

**`0x1208` is an effective injection quantity, not a raw pulse width.** Pairing
it against fuel per injection, `(MAF / (14.7·λ)) / (rpm/120 · 6)`:

| fit | n | slope (counts/g) | intercept | r² |
|---|---|---|---|---|
| all valid | 886 | 806,800 | 2113 | 0.762 |
| closed loop | 778 | 800,900 | 2031 | 0.787 |
| **steady state + closed loop** | **401** | **914,941** | **34** | **0.962** |
| steady state, zero-intercept | 401 | 916,416 | — | 0.962 |

The intercept is 34 counts, 0.23 % of mean, and forcing it to zero costs
nothing. The ratio PW/(g per injection) is flat to ±1.3 % across a 5.5x range
of fuel quantity (0.0061-0.0337 g), where a ~1 ms dead time would have roughly
doubled the low-fuel bin. **So the register excludes injector dead time**; any
fuel model built on it must add dead time separately or it will under-fuel at
short pulse widths.

Cross-check that raises confidence in both numbers: at the NT510-derived
`4e-4 ms/count`, 916,416 counts/g implies an injector flow of **~220 cc/min**,
which is right for a VQ40DE. Two independent methods, one plausible injector.
Note what this does *not* establish — fuel per injection was derived from the
same MAF the ECM uses, so this pins the register scale, not the injector's
physical flow rate.

**Target AFR: captured in closed loop, absent where tuning needs it.** This is
the first profile to produce commanded AFR at all (`tuning-did` disables Mode
01, which is what `project_bottleneck_is_experiment_design` recorded). PID 0x44
gives 1184 closed-loop samples, λ 0.871-1.164 (AFR 12.80-17.11), only 2 zeros.
But in open loop **227 of 385 samples read λ = 0** — the ECM's "not available"
— and the non-zero remainder sits at λ≈1.000, which is not a credible
enrichment command. **Open-loop/WOT target AFR is still not obtainable from
this ECM over Mode 01**, so the WOT fuel question is not unblocked by this
profile. That is an ECM limitation, not a capture-design one.

**Other results from the same drive.** AF sensor banks balanced: B1 vs B2
slope 0.9978, offset 0.012 V, r² 0.9885, n=1206. VVT tracking good on both
banks: measured vs commanded slope 0.981 / 0.978, r² ≈ 0.95, offsets
-0.36 / -0.46°, bank 2 marginally worse. `0x1209` is `0.2078 × 0x1208`
(r² 0.949) — the same quantity at a different scale, not an independent
signal, which weakens it further as a BFS provider.

**Broadcast decode coverage.** 29 distinct CAN IDs, 2.39 M frames at ~90 Hz.
Only four are bound: `0x6F7` (wheel speeds A/B/C), `0x1F9` (`ECM_RPM`), `0x23D`
(pedal/throttle volts), `0x551` (coolant). The unbound high-rate IDs — `0x2`,
`0x2DE`, `0x2A5`, `0x2D1`, `0x231`, `0x233`, `0x23E`, `0x794`, and the 46 Hz
group `0x280`/`0x284`/`0x285`/`0x29E`/`0x215`/`0x216`/`0x2A0` — now have 26
minutes of simultaneous Mode 01 engineering units to be identified against.
That is a binding opportunity, not a set of bindings: see AGENTS.md on the six
retracted findings before correlating anything.

**Two defects this drive exposed.**

1. *Fixed (`e0704c0`).* The first hybrid attempt that afternoon
   (`...T164035`) produced 76 s of data and then went DID-only. One `0x78
   responsePending` on a `10 81` fast exit was read as a refusal and latched
   the fallback off permanently; with `did_interval_seconds` (0.25 s) below
   `lapse_seconds` (5 s), the next window was always overdue when the lapse
   expired, so Mode 01 was starved to exactly zero rather than degrading. On
   *this* drive the exit failed transiently 11 times and recovered every time.
2. *Fixed.* 80 % of the drive's `FUEL_USED_L` and `CO2_KG` were `inf`, because
   `λ = 0` reached `maf / (14.7·λ)` and `inf` passes `notna()`, so the
   cumulative integrators carried it to the end of the session. Guarded at
   both the λ site and the integrator.

**Still open: the profile runs at a quarter of its stated rate.** Mode 01
achieved 1.43-1.59 Hz and the DIDs 0.91 Hz, against a 4 Hz target; the median
DID cycle is 1.07 s, not 0.25 s. There is also a one-off 36 s Mode 01 hole at
capture start. Neither is the latch bug, and neither is diagnosed yet — treat
`target_rate_hz: 4.0` in the profile as aspirational until it is.

## Phase 5 — the seven unpinned DID scales

Separate goal, same drive. These decode to raw counts today because
`scale`/`offset` are `null`, which is correct behaviour — an unpinned scale
must keep yielding counts with `unit: "counts"` rather than an invented
engineering value.

| DID | name | what would pin it |
|---|---|---|
| `0x1208` | `ECM_INJECTOR_PW_B1` | **Done 2026-08-07** — see the hybrid-drive results above. Pinned as **916,416 counts per gram of fuel per injection** (steady-state closed-loop only), and shown to carry no dead time. The `ms` scale is still open, because ms/count and counts/gram only convert through an injector flow rate nobody has measured directly. |
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

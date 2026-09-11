# H8 — continuous-DID profile on the truck, 2026-09-10

The last of the ten pending-hardware measurements. Raw evidence:
`2026-09-10-h8-continuous-did/` (20 s `/status` poll, plus the two analysis
scripts). Session
`_capture-tuning-did-2026-09-11T003700`, **25.2 minutes**, `tuning-did`
profile, `vehicle_state: engine_running` throughout, mixed city and highway
(GPS speed 0 to 113 km/h, altitude 1806 to 2024 m).

**Daemon revision `4c84ac3`, clean tree** — the first measurement on this truck
taken against a revision git can name. It carries D01.1 (out-of-process
scoring), D01.8 (batched feature rolling) and the D02 fixes. That matters for
every number below: the previous DID measurements were bounded by a starved
event loop, and it is no longer starved.

Mode 01 is disabled by this profile, so `coolant_temp` and `run_time` read
`null` in `/status` by construction. That is not a fault.

## The headline: the capacity was never the bus

| measurement | date | per-DID rate | total request rate |
|---|---|---:|---:|
| `tuning-hybrid-research`, warm idle | 2026-09-09 | 1.95 Hz | ~7.9 /s |
| `default`, driving | 2026-09-10 | 0.02 Hz | 4.0 /s |
| **`tuning-did`, driving** | **2026-09-10** | **10.4 Hz** | **116.8 /s** |

Over 1515 s the daemon issued **167,885 requests** and emitted **167,208
readings** — **116.8 requests/s sustained** against the profile's 180 /s
budget, with **41 timeouts (0.02 %)**, **0 negative responses** and **0 bus
errors**. Each of the eleven DIDs landed 15,699 to 15,781 samples: a spread of
0.5 %, so the round-robin is even to within noise.

That is **14.8x** the request rate measured at idle on 2026-09-09, when an
8 → 16 → 32 req/s budget ladder produced a flat ~7.9 /s and the conclusion was
that the daemon could not issue more. It can. The constraint was the event
loop, and halving the feature stage the same afternoon removed it. **No rate
default was raised to achieve this** — the 180 /s budget is the profile's own,
unchanged since it was written.

The bus remains nowhere near implicated: `can0` stayed ERROR-ACTIVE with zero
bus errors and zero restarts, and the ECM answered 99.98 % of what it was
asked at 116.8 requests per second.

## H8's actual question: fast dynamics vs slow context

All eleven DIDs are polled at one rate. They do not deserve one rate. Measured
over the drive, decoded through `data/dids.yaml`:

| DID | channel | moves | p95 abs rate of change | autocorr time |
|---|---|---:|---:|---:|
| 0x1225 | `ECM_AF_SENSOR_V_B1` | 84.6 % | 3.00 V/s | 4.8 s |
| 0x1226 | `ECM_AF_SENSOR_V_B2` | 84.6 % | 3.19 V/s | 4.8 s |
| 0x1219 | `ECM_RPM` | 82.3 % | 749 rpm/s | — |
| 0x1135 | `ECM_INTAKE_CAM_ANGLE_MEASURED` | 67.3 % | 67.5 degCA/s | 9.7 s |
| 0x1163 | `ECM_INTAKE_CAM_ANGLE_MEASURED_B` | 63.2 % | 60.0 degCA/s | 9.6 s |
| 0x120F | `ECM_THROTTLE_POS_SENSOR_1` | 59.7 % | 1.50 V/s | 9.6 s |
| 0x113A | `ECM_INTAKE_CAM_ANGLE_COMMANDED` | 33.6 % | 52.6 degCA/s | 9.6 s |
| 0x1101 | `ECM_COOLANT_TEMP` | **0.2 %** | 0.00 °C/s | — |

"moves" is the fraction of consecutive samples whose value changed at all.

**`ECM_COOLANT_TEMP` is the whole finding.** It changed on 0.2 % of samples and
covered 72 → 94 °C over 25 minutes, while consuming 15,748 requests — one
eleventh of the entire bus budget — to do it. At 0.1 Hz it would have captured
the same curve and returned **9.5 % of the profile's request budget** to the
channels that need it. Nothing else in the set is close: the next-slowest,
commanded cam angle, still moves on a third of its samples.

The two air-fuel sensors are the fast end and are the channels that would
actually use released capacity: at 84.6 % moves and a 4.8 s autocorrelation
time they are undersampled even at 10.4 Hz.

**Do not prescribe this to another vehicle.** The rate a channel deserves is a
property of that channel on that engine, and the method above — moves,
rate-of-change, autocorrelation time, measured over a real drive — is what
transfers, not the numbers.

## Three channels are polled but not calibrated

`ECM_INJECTOR_PW_B1` (0x1208), `ECM_INTAKE_VVT_SOLENOID_DUTY` (0x1110) and
`ECM_INTAKE_VVT_SOLENOID_DUTY_CANDIDATE_B2` (0x1139) carry `scale: null` and
`offset: null` in `data/dids.yaml`. Their raw bytes arrive intact at 10.4 Hz —
47,000 payloads across the drive — but nothing converts them to engineering
units, so they cannot be read as ms or percent. The profile spends 27 % of its
requests on them. Either establish their scale or drop them from the profile;
paying full rate for uninterpretable counts is the worst of both.

## The continuous session is not perfectly continuous

The session held open across the drive, with **3 drops in 25 minutes**:
`session_failures: 3` against **639 session-control requests, 636 answered,
3 timed out (0.47 %)**, and **630 keep-alives sent**. Each drop cost a gap of
**23 to 40 s** — visible as `obd_freshness_seconds` climbing to 23.2, 36.2 and
39.7 s at 18:43, 18:48 and 18:54 — because with Mode 01 disabled there is
nothing at all to poll while the session is re-established.

The recovery works and is automatic (`fast_exits: 3`, `fast_exit_failures: 0`,
`windows_opened: 3`, `windows_completed: 3`). What is disproportionate is the
cost: a 0.47 % session-control failure rate buys roughly 6 % of the capture in
complete blindness. **A mid-drive reading of this is easy to misread** — a
`/status` sampled inside one of those gaps shows 0.32 Hz and a `mode01` phase,
which looks like a collapsed capture rather than a 30-second recovery. The
per-DID rate in a 60 s window swings with sample phase; only the whole-session
count (15,7xx samples each) settles it.

Worth fixing, not urgent: the retry interval after a session drop is the
profile's 60 s window interval, which is why a 3 s recovery waits out most of a
minute.

## Response widths: D02.4 independently re-confirmed, five more recorded

This capture is an independent check on the widths recorded earlier the same
day, taken from a different vehicle session on a different daemon revision.
**Every recorded width matched exactly** — 0x1101 at 1 byte; 0x1208, 0x120F,
0x1219, 0x1225, 0x1226 at 2 — across 15,7xx payloads each, with no other width
observed once.

Five more were measured and written to `data/dids.yaml`: 0x1110, 0x1135,
0x1139, 0x113A and 0x1163, all **1 byte**, confirmed both across 6 to 23 local
sessions (40,609 to 42,130 payloads each) and on this drive. **Every DID in
both the `default` and `tuning-did` profiles now has a provenance-backed
width**, so multi-DID batching is no longer blocked for either. It remains
switched off (`obd_did_batch_max_dids` defaults to 1); enabling it is a
separate comparison and would have confounded this one.

## Health under the load

| quantity | value |
|---|---:|
| tick `total` median | 320 ms |
| ticks missed | 3 of 816 |
| storage queue high water | 1,225 / 10,000 |
| dropped records | 0 |
| CPU temperature | 61–63 °C |
| throttling since boot | none |

The inference tick ran at a **320 ms** median under roughly seven times the
default profile's channel load, against **659.7 ms** on the lighter `default`
profile on 2026-09-10 before D01.8. That is a stronger D01 Phase 3 result than
the like-for-like comparison would have been, and it was free.

## What this closes and what it opens

Closes **H8**, and with it the last of the ten pending-hardware measurements.

Opens three things, none blocking:

- Lower `ECM_COOLANT_TEMP` to ~0.1 Hz in the `tuning-did` profile and give the
  released 9.5 % to the air-fuel sensors. Re-measure coverage after.
- Establish scale/offset for 0x1208, 0x1110 and 0x1139, or drop them.
- Shorten the post-drop session retry so a 3 s recovery does not wait 60 s.

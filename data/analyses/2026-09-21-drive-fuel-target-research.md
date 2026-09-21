# 2026-09-21 drive — fuelling, timing and airflow on ZS44A-20260920

Session `_capture-fuel-target-research-2026-09-21T195844`, 19.9 min, engine
running throughout, daemon build `6ef51b4b`, calibration `ZS44A-20260920`
(registry; one Fuel Targets byte changed from `ZS44A-k106`). GPS altitude
1,933–2,042 m. Pulled to the laptop by tar stream; analysis run there.

Recorded under `fuel-target-research`, not `rom-trace`: `rom-trace` opened its
Mode-22 session and then sent no requests and no keepalives for over a minute
(`phase: window`, `windows_opened: 1`, `requests_sent: 0` on all 24 requests),
recovering the moment the capture was stopped. Cause not yet found; see the
open item at the end.

## What the capture could and could not answer

| wanted | present |
|---|---|
| rpm, MAF, load, timing, commanded φ (PID 0x44), λ (PID 0x24), fuel status | Mode 01, 4 Hz |
| commanded φ (RAM), ignition timing (RAM), engine speed (RAM) | memory windows |
| A/F sensor volts B1, air charge, throttle, `0x1134` | Mode 22, 2 Hz |
| **baro, intake-air temperature, coolant** | **not polled** — fixed for future captures, below |
| short/long fuel trims | not polled by this profile |
| bank 2 A/F, injectors, cam, ROM map traces | not polled by this profile |

The WOT content is 20+ short accelerations (1–11 s), not steady gear pulls.
The longest ran 1,350 → 4,288 rpm in 11.2 s; only two events passed 4,000 rpm,
and the 5,200 rpm / BFS 18.20 cell edited in the 2026-09-20 flash was not
reached at steady state (peak 5,150 rpm, one sample).

## Known answers first

Two instruments, same quantity, paired nearest within 120 ms:

- **Timing, RAM `0xFFFF11C7` vs PID 0x0E:** n = 888, median difference 0.0°,
  81 % within 1°. The spread is pairing skew on a fast quantity.
- **Commanded φ, RAM `0xFFFF26BE` vs PID 0x44:** n = 885, ratio median
  1.0000 (p5 0.981, p95 1.149). The tails are target transitions landing
  between the two reads.

Both RAM channels therefore read what their DIDs/PIDs read, on the new
calibration.

## Fuelling at WOT

`delivery_error` (open loop, enrichment, load ≥ 85 %, same-instant pairing),
measured on the ECM's own λ:

| rpm | n | commanded AFR | measured AFR | error |
|---|---|---|---|---|
| 2,500–3,000 | 6 | 12.63 | 12.87 | +1.9 % |
| 3,000–3,500 | 9 | 12.93 | 13.40 | +3.6 % |
| 3,500–4,000 | 7 | 12.84 | 13.29 | +3.6 % |
| 4,000–4,500 | 1 | 12.22 | 12.67 | +3.7 % |
| 4,500–5,000 | 2 | 12.10 | 12.73 | +5.2 % |
| 5,000–6,500 | 1 | 11.91 | 12.32 | +3.5 % |

Pooled +3.3 %, band-averaged +3.6 %, n = 26.

The second instrument — A/F sensor volts through the calibrated transfer,
against the RAM target (semantic path, 1 s grid, no load gate available) —
reads +4.2 % and +4.3 % in the two well-populated bands (3,000–4,000 rpm,
n = 17) and scatters from −3.5 % to +15 % where n ≤ 4. The two instruments
agree where there is data to agree on.

**Reading.** WOT runs about 3.5–4 % lean of commanded at 3,000–4,000 rpm on
both instruments, above the ±2.4 % single-cell floor. That matches the
+3.0 % residual of August, which equalled the 48-vs-51 psi fuel-supply offset.
Nothing here separates a calibration error from a delivery shortfall; the
August recommendation stands — confirm supply pressure before moving K, or
the offset gets baked into the injector constant.

**Caveat on the sample set.** Most enrichment samples come from tip-ins. The
measured mixture lags the commanded one by the transport delay, so the first
samples of each event read lean; the 2,500–3,000 band's +15 % on the voltage
path is almost certainly that. Steady gear pulls would remove it.

## Ignition timing at WOT

Final timing (PID 0x0E, with rpm/MAF within 40 ms), load ≥ 85 %:

| rpm | n | events | median | min | max |
|---|---|---|---|---|---|
| 1,500–2,000 | 47 | 11 | 9° | 3° | 14° |
| 2,000–2,500 | 43 | 13 | 11° | 5° | 15° |
| 2,500–3,000 | 70 | 11 | 11° | 2° | 13° |
| 3,000–3,500 | 65 | 12 | 10° | 5° | 13° |
| 3,500–4,000 | 28 | 8 | 9° | 8° | 11° |
| 4,000–4,500 | 7 | 2 | 10° | 9° | 10° |
| 4,500–5,200 | 7 | 2 | 13° | 12° | 16° |

About 10° BTDC across the whole band, against 24.6° logged at 3,000–3,500 rpm
/ high load on the stock calibration (`engine-tuning-support.md` 3.5). This is
consistent with the August finding that the loaded cells lost 7–11° after the
Osiris flash, and with its plan: add 1–2° per iteration in the 3,000–4,500 rpm
high-load cells, and do not change fuel in the same flash.

Per-event medians in the same band vary by 2–5°, with low outliers of 3°
(1,500–2,000) and 7.5° (2,500–3,000). Those are tip-ins, where transient
spark reduction is normal, so they are not evidence of knock retard; a steady
pull that dips below its neighbours in a cell that was advanced would be.

## Airflow

| rpm | MAF g/s | air per cylinder-event | VE* |
|---|---|---|---|
| 1,500–2,000 | 41 | 0.46 g | 74 % |
| 2,000–2,500 | 54 | 0.49 g | 79 % |
| 2,500–3,000 | 72 | 0.52 g | 83 % |
| 3,000–3,500 | 86 | 0.54 g | 86 % |
| 3,500–4,000 | 96 | 0.54 g | 86 % |
| 4,000–4,500 | 116 | 0.55 g | 88 % |
| 4,500–5,200 | 125 | 0.53 g | 86 % |

\*VE rests on two things this capture did not measure: baro, estimated at
80.0 kPa from GPS altitude, and intake temperature, assumed 25 °C (each
+10 °C would raise VE ~3.4 %). Short tip-ins also inflate MAF while the
manifold fills. Treat the curve's shape as the finding — rising to a plateau
around 3,500–4,500 rpm, no kink — and the absolute numbers as indicative. No
sign of a restriction.

## Changes made as a result

- **Every capture profile now carries baro, IAT and coolant**
  (`CAPTURE_CONTEXT_MODE01` in `src/collection/capture_profiles.py`), at
  ~1 Hz whatever the profile's own rate; all-DID profiles carry the IAT and
  coolant DIDs instead. A test holds every shipped profile to it. Baro has no
  known DID, so continuous all-DID captures still take it from the adjacent
  default session. Seed revision 5.
- **`read_channels` no longer loses a session that opens on fuel cut.** The
  commanded-AFR convention probe took the first value, which could be φ = 0.
- **`delivery_error` refuses a sensor-voltage channel** as its measurement,
  after returning −87 % for volts compared against AFR during this analysis.

## Still open

- **`rom-trace` stalls in its continuous session.** Worked for four minutes
  on the morning of 2026-09-21; stalls now. Differences: two 1 Hz DIDs added
  (`0x1134`, `0x1201`), and the other session's commits.
- **Memory windows ran ~2× their configured rate** under this profile
  (`mem:fuel_target` 251 sends in 25 s against 2 Hz). Harmless here; a rate
  bug.
- **A drive with steady WOT gear pulls** (3rd, 2,500 → 5,500 rpm, ≥ 3) is what
  would pin the fuelling figure, reach the edited cell, and make timing
  retard distinguishable from tip-in reduction.

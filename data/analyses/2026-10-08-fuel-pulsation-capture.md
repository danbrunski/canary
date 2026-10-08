# Fuel-rail pulsation, first full-rate capture (2026-10-08)

**Status:** measured. The gauge-step finding has a known-answer check behind
it (the images below). The plumbing reading of order 1.5 is a hypothesis.

## Capture

- Session `_capture-fuel-pulsation-2026-10-08T210012` (Pi, pulled to
  `~/canary-data`). Mostly highway, 21:00–21:45Z. Profile `fuel-pulsation`
  (`analog_full_rate`), with no deliberate holds: steady stretches were found
  from rpm.
- USB ADC firmware v2. 1,371,355 full-rate samples at a measured
  **503.19 Hz**, missing **50** (13 sequence gaps, 0.004 %). A quick check
  reported 494 Hz; that was a count over wall-clock time, not the rate.
- The first 13 rows (120 ms) were the switch-over, stored as 5-sample means.
  They made `tools/analyze_analog_pulsation.py` refuse the whole session.
  Fixed: edge rows stored at another rate are now skipped and counted;
  rows mixed in mid-capture are still refused.
- Bands with steady windows: 759 (1), 2202 (2), 2413 (52), 2642 (31),
  2996 (34) and 3219 rpm (6).

## Finding 1: the AEM gauge's analog output steps at ~176 Hz

The change-energy spectrum has a line at **176.4 Hz in all six bands**, from
759 to 3219 rpm, so it is fixed in Hz and not an engine order. Two causes are
possible: an output staircase (a held DAC) or a constant pressure tone at
88.2 Hz (a pump). The second is ruled out by two observations:

- **No 88 Hz line in the pressure spectrum** in any band.
- **Images.** A held output turns a real line at `f` into an image at
  `176.4 − f`, and adding a tone does not do that. The images are there,
  tracking the order-1.5 line exactly:

  | rpm | order 1.5 | 176.4 − f | observed |
  |---|---|---|---|
  | 2202 | 54.5 Hz | 121.9 | 120.4, 121.9 |
  | 2413 | 60.4 | 116.0 | 114.5, 116.0 |
  | 2642 | 64.9 | 111.5 | 110.1, 111.1 |
  | 2996 | 74.2 | 102.2 | 100.2, 101.7 |
  | 3219 | 80.1 | 96.3 | 94.3, 95.8 |

So the gauge updates its analog output about every **5.7 ms**. Consequences:

- **The gauge resolves pulsation below ~88 Hz only.** Anything faster is folded
  by the gauge before the ADC sees it, and no filter on our side can undo
  that. Order 1.5 stays below 88 Hz up to ~3500 rpm. Order 3 is above 88 Hz
  from ~1760 rpm, so above that it cannot be measured with this gauge.
- **Every pressure line above 88 Hz in this capture is an image.** The lines
  first read as "order ~2.9–3" at 2400 rpm and up are images of order 1.5,
  not the injection rate. The analysis now labels such lines (`image of`,
  `above the gauge's limit`), so that reading can't be made again.
- Even below 88 Hz the hold attenuates amplitude as `sinc(f/176.4)`: about
  −3 dB at 80 Hz. That accounts for part of the order-1.5 amplitude falling
  with rpm below.
- To study order 3 at driving speeds, the pressure needs a sensor read
  directly (the bare transducer, not the gauge's re-generated output), or a
  gauge whose output is continuous.

## Finding 2: order 1.5 dominates the rail

Amplitudes are in kPa rms (psi in brackets). Each band's three or so
order-1.5 peaks are one line spread by the 300 rpm band width.

| rpm | order 1.5 | others |
|---|---|---|
| 759 (idle, 1 window) | 0.22 [0.03] | **order 4.5: 0.87 [0.13]**, order 3: 0.36 [0.05] |
| 2202 (2 windows) | **4.9 [0.71]** | ~6 Hz: 0.7 |
| 2413 (52) | **1.6 [0.24]** | |
| 2642 (31) | **1.4 [0.20]** | order 1.6: 0.46 |
| 2996 (34) | **0.86 [0.12]** | order 1.0: 0.34 |
| 3219 (6) | **0.66 [0.10]** | |

The 2202 rpm figure comes from two windows. It may be a resonance or a
transient, and needs more steady time there before it is trusted.

**Reading (hypothesis).** The VQ40DE injects sequentially, one event every
120° of crank, alternating banks, so each bank's three injectors fire 1.5
times per revolution. If the sensor saw both banks equally, their pulses
would sum to order 3 and order 1.5 would cancel. A dominant order 1.5, with
order 4.5 and other odd multiples of 1.5 strong at idle, means the sensor is
coupled far more to one bank's rail than the other. That fits a gauge teed
in at one rail's feed, with the crossover between the rails damping the far
bank. canary-90 read it the same way.

**Which bank: bank 2** (operator, 2026-10-08). The fuel line enters on the
driver side, which on the Xterra is bank 2 (cylinders 2-4-6; bank 1 is 1-3-5,
passenger side), and the rail loops round to bank 1. So the gauge mostly hears
bank 2's injectors. For mean pressure and airflow droop that doesn't matter.
For any bank-to-bank fuelling comparison it does: this pressure is bank 2's
rail, not bank 1's. No analysis compares banks against it yet. When one does,
the sensor's position should become a declared field it has to check, not
just a note.

Not established yet:
- whether the 2202 rpm amplitude is real;
- what the slow ~6–7 Hz content (order 0.15–0.19, at every rpm) is.
  It could be the in-tank regulator's own cycling.

## Reproduce

```bash
.venv/bin/python -m tools.analyze_analog_pulsation \
    ~/canary-data/sessions/nissan-xterra-2008/_capture-fuel-pulsation-2026-10-08T210012
```

Tests for the edge rows, the images and a constant pump tone are in
`tests/test_analog_pulsation.py`.

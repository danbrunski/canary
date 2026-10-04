# Fuel pressure, first drive — 2026-10-04

The first drive with the AEM 30-0301 analog output on the NOYITO USB ADC at
100 Hz (`sensor:FUEL_PRESSURE`, `docs/aem-fuel-pressure-analog-input.md`).
Sessions `2026-10-04T190056` (19:40:26–19:42:45Z, before a Pi reboot) and
`2026-10-04T194342` (to 20:16Z). Profile `default`, so OBD Mode 01 at ~1.5 Hz
for the fast tier. No full-throttle pull: the highest airflow was 118 g/s at
4,500 rpm. Pressures below are the analog reading, which sits a constant
+1.8 psi above the gauge face; that offset is still unresolved.

## Collection

- **205,212 pressure rows**, median spacing 10.0 ms after the reboot.
- **One stall, at start-up:** `UsbAdcStalledError` 33 s after boot
  (19:44:27Z), reconnected 10 s later; a 24.8 s hole in total.
- **Before the reboot**, a ~0.7 s hole every ~14 s (7 in 140 s). There were
  none after the reboot. The old process had been up since 19:00:54Z, and the
  cause was not looked into.
- **113 `FUEL_PRESSURE_FAULT` rows (`below_window`) at 19:49:40.1–41.3Z.**
  This was a crank, not a fuel fault. The engine stopped at 19:49:11–14Z (CAN
  rpm 794 → 0; OBD was passive) and restarted at 19:49:40Z. The gauge's output
  fell below 0.5 V while cranking, and the window gate recorded it as a fault
  instead of a pressure. The gate behaved as designed.

## Findings

**1. Pressure falls with airflow, by ~1.9 psi at 107 g/s.** Each OBD sample
was paired with the mean pressure within ±0.25 s of it: 1,843 engine-running
pairs.

| MAF g/s | pairs | median psi |
|---|---|---|
| 0–5 | 297 | 50.79 |
| 5–10 | 463 | 50.81 |
| 10–15 | 115 | 50.59 |
| 15–20 | 74 | 50.54 |
| 20–30 | 152 | 50.46 |
| 30–40 | 218 | 50.35 |
| 40–60 | 382 | 50.19 |
| 60–100 | 117 | 49.73 |
| 100–118 | 8 | 48.87 |

- **The drop is not linear: it steepens.** It is −0.015 psi per g/s
  overall, and ~−0.022 psi per g/s above 50 g/s.
- **It is not a drift over the drive.** Pressure at low airflow (MAF < 10 g/s)
  moved only from 51.0 psi cold to 50.7 psi warm. Adding elapsed time and
  module voltage to the fit takes the residual from 0.176 to 0.152 psi.
- **What it means for fuelling:** −3.8 % pressure at 107 g/s is −1.9 %
  injector flow (flow ∝ √ΔP). This ECM has no fuel-pressure input. Whether
  that is uncorrected depends on what the calibration assumed: a factory map
  may already absorb a new pump's droop, and a tired pump that droops more
  would show up as exactly this kind of lean. Either way, it is already about
  half of the +2.3…+4.1 % WOT lean measured on 2026-09-21/23, at well below
  WOT airflow.
- **Leaning answer to NA2's question** ("steady pressure says MAF curve,
  falling pressure says delivery"): falling. It needs a real 3rd-gear pull to
  settle how far.

**2. Pressure holds after shutdown.** When the engine stopped, pressure fell to
47.3 psi within ~5 s, then decayed only to 46.7 psi over the next 22 s. After
the restart it was back to 50.5 psi within 2 s. The system holds residual
pressure well.

**3. The 100 Hz trace carries real rail pulsation, and it is aliased.**

- **Size:** with the pump off, the residual is 0.024 psi rms, about one ADC
  step (0.030 psi). Running, it is 0.12–0.38 psi rms, almost all of it above
  20 Hz.
- **Source:** in 96 of 99 steady 8 s windows, the strongest tone above 5 Hz is
  within 1 Hz of the 100 Hz alias of an engine-order harmonic (rpm/40, rpm/20,
  or 3·rpm/40). That would happen by chance in about 13 % of windows.
  - The fundamental, rpm/40 Hz, is 3 pulses per engine cycle: one bank's
    injectors. Presumably the sender sees one rail.
  - At idle the 2nd harmonic dominates: 40 Hz at 800 rpm.
- **The gauge's analog output has the bandwidth to pass this.** It is not a
  slow stepped output, which answers the "gauge's own update rate" question
  in the AEM doc.
- **Consequences:**
  - Any per-sample use must average over ≥ 0.5 s, or over whole engine
    cycles. The mean is unaffected.
  - Studying the pulsation itself needs a faster sample rate or an
    anti-alias filter, because the firmware's 10 ms boxcar does not
    attenuate 50 Hz.

## Method

Scripts were scratch, not committed. Rows were read from the pulled sessions
in `~/canary-data`. For the pairing, each OBD channel was taken as-of the
nearest sample within 0.6 s, and pressure as the mean over ±0.25 s with
≥ 30 samples. Spectra are Hann-windowed FFTs of the de-meaned 100 Hz series.

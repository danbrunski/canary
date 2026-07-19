# IMU + GPS integration — motion-aware auto-tagging and grade de-confounding

Adding a 3-axis IMU and a GPS receiver to the Pi so the daemon can label
runs by *what the vehicle is doing* (hard accel, hard brake, cornering,
stopped, highway cruise, launch) without a human in the second terminal,
and so the load-based behavior surfaces stop being confounded by road
grade.

This is the design contract before any code. The conclusion up front:
the architecture absorbs two new sources cleanly because the storage and
wide-frame layers are already source-agnostic; the real work is the two
collectors, a gravity/heading resolution step, high-rate decimation, and
the auto-tag pass. None of it is a rewrite.

## Why this is worth doing

Two distinct payoffs, ranked:

1. **Clean transient events.** `obd:SPEED` over the Canable OBD path is
   ~1–5 Hz with jitter. `src/analysis/derived.py::_accel_metrics` already
   has to scan launches and abandon noisy ones because it differentiates
   that coarse trace. A 100+ Hz accelerometer gives longitudinal/lateral
   g *directly* — no differentiation of a slow signal. Hard-accel /
   hard-brake / cornering get an order of magnitude cleaner, with precise
   timing the OBD trace cannot provide.

2. **Grade de-confounding for behavior models.** `timing_advance_vs_rpm_load`
   and `fuel_trim_vs_rpm_load` in `data/behavior_models.yaml` bin on
   engine load. Load on a 6 % grade ≠ load on the flat at the same RPM;
   that variance currently lands in the cell `std` and widens the
   deviation bands. GPS altitude delta (better: a barometric altimeter —
   see Hardware) gives a grade channel that can either filter training
   data to near-flat segments or become a third context axis. This
   tightens the surfaces the alerting actually keys off.

Auto-tagging itself (1) is the headline; (2) is a structural bonus that
falls out of the same hardware.

## The physics, settled

What a 3-axis accelerometer can and cannot self-calibrate, because it
dictates the resolution pipeline:

* **Tilt (pitch/roll) — self-calibrating.** During quiescent windows
  (OBD speed ≈ constant, RPM steady, GPS heading constant) the accel
  vector is ≈ pure 1 g; its direction in the sensor frame is "down."
  Equivalently, a low-pass of the accel vector tracks gravity because
  vehicle acceleration is transient (high-freq) and gravity is DC.
  Re-estimating continuously also absorbs bias/temperature drift —
  dynamic beats one-shot, and is the *only* correct version (a one-shot
  calibration on a sloped driveway is permanently biased by the
  driveway).
* **Heading (yaw) — NOT self-calibrating.** Gravity is rotationally
  symmetric about vertical; it carries zero information about which
  horizontal direction is "forward." Resolving longitudinal vs lateral
  needs an external horizontal reference: GPS course during a straight
  pull. (A magnetometer is unreliable in a steel vehicle full of current
  — do not depend on the 9th axis for heading.)
* **Accel vs brake sign — free from OBD.** We already have `obd:SPEED`.
  Its derivative's sign says speeding-up vs slowing-down; the IMU
  supplies the clean magnitude and exact timing. Correlating the two
  yields signed hard-accel / hard-brake tags from **IMU + OBD alone, no
  GPS**. GPS is required specifically for *lateral* (peeling cornering
  out of the horizontal plane — the yaw problem) and for *grade*.
* **Mount tilt vs road grade — separable only over time.** A single
  static reading cannot tell a crooked mount from a parked-on-a-hill
  reading. A running estimate across varied terrain does: mount tilt is
  the constant component, grade the variable one. Another argument for
  continuous estimation.

Division of labor: **IMU + OBD** → longitudinal events, self-calibrating.
**GPS** → cornering split, grade, bus-independent stopped/moving, time
discipline.

## Two new sources, slotted into the existing model

`src/telemetry.py` defines `TelemetrySource = Literal["obd", "can",
"decoded", "gps"]` (the `gps` member and `.gps()` factory shipped with
Phase 1 below) and the wide-frame layer keys columns off
`source + ":" + channel` (`src/preprocessing/loader.py:38`). The Parquet
schema (`TelemetryRecord.parquet_row`) is long-format and entirely
source-agnostic — **new sources need no storage migration**. The ripple
is bounded and enumerable:

| Touch point | Change |
|---|---|
| `src/telemetry.py` | ✅ `"gps"` + `.gps()` shipped. Remaining: extend the Literal with `"imu"`; add `.imu()` mirroring `.obd()` |
| `src/preprocessing/live.py:102` | The `obd`/`can`/else router gains explicit `imu`/`gps` handling instead of falling through the catch-all |
| `src/preprocessing/loader.py` | None structurally — `to_wide` pivots on `source_channel` already. Decimation (below) is a new pre-pivot step |
| `src/config.py` | New `ImuConfig` / `GpsConfig` blocks alongside `CollectionConfig`, same `_Strict` pattern |
| Parquet storage | None — `parquet_row()` already columnar and source-neutral |

New channels by convention: `imu:ACCEL_X/Y/Z`, `imu:GYRO_X/Y/Z`,
`imu:LIN_ACCEL_LON/LAT` (gravity-removed, frame-resolved),
`gps:SPEED`, `gps:LAT`, `gps:LON`, `gps:ALT`, `gps:COURSE`, `gps:FIX`,
`derived:GRADE`.

## The collectors

Two new modules under `src/collection/`, each emitting `TelemetryRecord`
and supervised by the same reconnect pattern as `obd_collector.py` (a
sensor that browns out over vibration must not kill the daemon):

```
            ┌──────────────┐   I²C    ┌───────────────────────┐
            │ IMU (accel + │─────────▶│ src.collection.       │
            │ gyro)        │  100 Hz  │ imu_collector         │──┐
            └──────────────┘          └───────────────────────┘  │
            ┌──────────────┐   UART   ┌───────────────────────┐  │ TelemetryRecord
            │ GPS receiver │─────────▶│ src.collection.       │──┤ (source="imu"/"gps")
            │              │  10 Hz   │ gps_collector         │  │
            └──────────────┘          └───────────────────────┘  │
                                                                  ▼
                              raw rows ─▶ Parquet (long, unchanged schema)
                                       └▶ src.preprocessing.live ─▶ decimate ─▶ wide frame
```

`imu_collector.py` runs its own thread at the sensor rate. `gps_collector.py`
parses NMEA (via `gpsd` or `pynmea2`). Both honor the `.venv` (3.12)
toolchain and add their deps through `scripts/install_deps.py` /
`pyproject.toml`.

Config sketch (mirrors `CollectionConfig`'s `Literal` source pattern so
"no hardware" degrades cleanly, same as `obd_source: disabled`):

```yaml
imu:
  source: bno055        # bno055 | mpu6050 | icm20948 | disabled
  i2c_bus: 1
  sample_rate_hz: 100
  mount_orientation: auto   # auto = dynamic gravity resolution; or fixed quaternion
gps:
  source: gpsd          # gpsd | serial | disabled
  device: /dev/serial0
  baud: 9600
  discipline_clock: true    # feed PPS to chrony; use GPS time pre-NTP
```

## Gravity / frame resolution

A small module (`src/preprocessing/imu_frame.py`) turns raw
`imu:ACCEL_*` into frame-resolved `imu:LIN_ACCEL_LON/LAT`:

1. **Gravity estimate** — low-pass of the accel vector, gated to
   quiescent windows detected from `obd:SPEED` steadiness + GPS heading
   constancy. Subtract to get linear (motion-only) acceleration; this
   removes pitch/roll without any manual calibration.
2. **Heading resolution** — during straight GPS-confirmed pulls, rotate
   the horizontal linear-accel vector so its principal axis aligns with
   GPS course. Persist the resulting yaw offset per session; it is the
   mount's rotation about vertical and is otherwise unobservable.
3. **Sign** — take longitudinal sign from the OBD speed derivative, not
   the IMU, so accel/brake labelling works even before heading converges.

**If the chosen module is a BNO055, steps 1–2 collapse into a hardware
read** — its on-chip fusion outputs gravity-removed linear acceleration
and an absolute orientation quaternion directly. We still take *sign*
from OBD and *heading* from GPS (the BNO's magnetometer-based yaw is
unreliable in a car), but the gravity-vector math we discussed is then
the sensor's job, not ours. This is the single biggest argument for the
BNO055 over a raw MPU-6050.

## Decimation into the wide frame

A 100 Hz IMU must **not** flow raw into `to_wide` — it would dwarf the
1–5 Hz OBD rows and bloat the pivot. The preprocessing pipeline gains a
per-resample-tick aggregation step (alongside the existing resampler):
for each output tick emit `imu:ACCEL_LON_MAX`, `imu:ACCEL_LON_RMS`,
`imu:JERK_MAX`, `imu:ACCEL_LAT_MAX`. Raw high-rate samples still land in
Parquet (long format, cheap, source-agnostic) for offline forensics; only
the wide/feature frame is decimated. This keeps the IF/LSTM feature
vector the same width-class it is today.

## The auto-tagger

The output is the existing contract: `src/collection/tags.py::write_tag`
appends `{"t", "kind", "label", ...}` to `<session>/tags.jsonl`, and tag
timestamps already share `time.time()` wall-clock with `frames.log`. The
auto-tagger writes **the same records the manual `scripts/tag.py`
writes** — so every existing consumer (analyzer windows, dataset export,
correction flow) works unchanged. It runs two ways:

* **Live**, inside the daemon, emitting events as thresholds trip.
* **Offline**, a pass over a session's Parquet (`scripts/autotag.py
  --session ...`), idempotent, re-runnable as thresholds are tuned.

Event taxonomy (label, trigger):

| Label | Trigger | Sensors |
|---|---|---|
| `hard_accel` | `imu:ACCEL_LON_MAX` > θ_a **and** d(`obd:SPEED`)/dt > 0 | IMU + OBD |
| `hard_brake` | `imu:ACCEL_LON_MAX` > θ_b **and** d(`obd:SPEED`)/dt < 0 | IMU + OBD |
| `cornering` | `imu:ACCEL_LAT_MAX` > θ_c | IMU (heading-resolved) |
| `launch` | `obd:SPEED` ≈ 0 → sustained `hard_accel` | IMU + OBD |
| `stopped` | `gps:SPEED` ≈ 0 (bus-independent) | GPS |
| `highway_cruise` | `gps:SPEED` > θ_h sustained, low lat/lon accel variance | GPS + IMU |

Thresholds live in config, not code, so they tune without a deploy
(same philosophy as the behavior-model deviation bands). Each tag
carries the triggering peak value as a `**fields` extra (e.g.
`peak_g=0.42`) for later threshold review.

## Segmenter extension

`segment_operating_modes` (`src/preprocessing/segmenter.py`) and
`OperatingModeSpec` (`src/config.py`) currently bound only `rpm` /
`speed` / `throttle`. Adding accel-aware modes is a localized extension,
not new machinery:

* `OperatingModeSpec`: add `accel_lon_min/max`, `accel_lat_min/max`.
* `DEFAULT_SIGNAL_COLUMNS`: add `accel_lon → imu:ACCEL_LON_MAX`, etc.
* `_required_signals` / `_bounds`: add the new signal arms.

New modes (`launch`, `hard_brake`, `cornering`) then become threshold
specs in YAML exactly like the existing `idle` mode, and feed
`operating_mode`-scoped behavior models the same way `idle_rpm_vs_coolant`
already uses `operating_mode: idle`.

## Hardware recommendations

### IMU — recommended: **Bosch BNO055** (Adafruit/equivalent breakout)

| Module | Axes | Fusion | Bus | ~Cost | Verdict |
|---|---|---|---|---|---|
| **BNO055** | 9 | **on-chip** (linear-accel + quaternion out) | I²C / UART | $20–35 | **Recommended.** Does the gravity-vector removal in firmware — exactly the dynamic calibration we want — so our code only does heading (GPS) + sign (OBD). |
| ICM-20948 | 9 | none (SW fusion) | I²C / SPI | $5–15 | Good budget pick if you want raw control; you implement the low-pass/complementary filter in `imu_frame.py`. Current-gen TDK part. |
| MPU-6050 | 6 | none | I²C | $2–4 | Cheapest, ubiquitous libraries, fine for harsh-event *magnitude*. No magnetometer (irrelevant here). Aging; clone quality varies. |
| ADXL345 | 3 (accel only) | none | I²C | $2–4 | Not recommended — no gyro means gravity tracking during sustained accel is harder and rotation context is lost. |

The BNO055 has a well-known caveat: its **I²C clock-stretching breaks
the Raspberry Pi's hardware I²C**. Run it in **UART mode** on the Pi
(or use a software-I²C overlay). Flag this in `pi-setup.md` when wiring.
Whatever the choice, mechanical rigidity matters more than the sensor —
mount the IMU to the fixed Pi enclosure, not anything that can shift.

### GPS — recommended: a **u-blox M8N/M9N/M10-class 10 Hz** module, or **Adafruit Ultimate GPS (MTK3339)** for Pi-friendliness

1 Hz NEO-6M-class modules are too slow to help transients or grade; get
10 Hz. Multi-constellation (u-blox M8+/M10) markedly improves
urban-canyon fixes. A **USB GPS dongle** is the lowest-integration option
(`/dev/ttyACM0`, `gpsd`); a UART module frees USB but needs the Pi serial
console disabled. Prefer one with a **PPS pin** — see below.

### Grade — add a barometric altimeter: **BMP390** (or MS5611), I²C, ~$5

GPS altitude is ±10–30 m and noisy — poor for grade. A baro's *relative*
altitude over tens of seconds is low-noise; differentiating it against
GPS ground speed yields a far cleaner `derived:GRADE` than GPS altitude
alone. This is the channel that de-confounds the load surfaces (payoff
#2), so it is not optional if (2) matters.

### Time discipline

The truck has no Wi-Fi, so the Pi boots without NTP. GPS provides an
absolute time reference, and a **PPS pin into `chrony`/`gpsd`** disciplines
the Pi clock to sub-millisecond. Since `tags.jsonl` and `frames.log`
both stamp with `time.time()`, a disciplined clock directly tightens
cross-source alignment and gives sessions a correct wall time even when
they start pre-NTP. Worth the one extra wire.

Bus budget: IMU + baro on I²C bus 1, GPS on UART (or USB). All 3.3 V
logic, low current, off the Pi rails. No conflict with the Canable
(SocketCAN/USB).

## Phased rollout

1. **GPS first, lowest risk.** ✅ **Shipped** —
   `src/collection/gps_collector.py` + `gps_nmea.py`, `gps:*` channels,
   `GpsConfig`, satellite/HDOP speed quality-gate, and
   `src/analysis/gps_tracker.py` (trip odometer) are live. The
   new-source plumbing (telemetry Literal, live router, Parquet, wide
   frame) is validated end to end. The auto-tagger (`scripts/autotag.py`)
   is **not** built yet — it moves to Phase 2 alongside the IMU tags.
2. **IMU + frame resolution.** `imu_collector.py`, decimation step,
   `imu_frame.py`, the `hard_accel` / `hard_brake` / `cornering` /
   `launch` tags. Recommend BNO055 to keep step 1–2 of frame resolution
   out of our code.
3. **Grade + behavior-model payoff.** Baro, `derived:GRADE`, then either
   filter behavior-model training to near-flat segments or add grade as a
   context axis, and re-fit. Measure deviation-band tightening to confirm
   (2) actually paid off.

## Risks and honest limits

* **Heading convergence needs straight pulls.** Until the per-session
  yaw offset converges, `cornering` may bleed into `hard_accel`. The
  OBD-sign trick keeps longitudinal labelling correct meanwhile;
  cornering is the one that waits.
* **Cheap GPS dropout** (tunnels, cold start, canyon) — treat `gps:FIX`
  as a first-class channel; auto-tag rules must degrade, not lie, when
  fix is lost. The IMU is the fast sensor, GPS the slow absolute one;
  never treat GPS as ground truth for fast transients.
* **Threshold tuning is empirical.** θ values are vehicle- and
  mount-specific; the offline re-runnable tagger and the
  peak-value-in-tag-fields design exist precisely so tuning is a data
  exercise, not a redeploy.
* **The steady-state segmenter is already adequate.** This hardware
  earns its place on transients, bus-independent robustness, and grade —
  not by replacing OBD-derived idle/cruise detection.

## Where the code lives (proposed)

| Layer | Module |
|---|---|
| IMU capture | `src/collection/imu_collector.py` (new) |
| GPS capture | `src/collection/gps_collector.py` (new) |
| Source contract | `src/telemetry.py` (extend Literal + factories) |
| Config | `src/config.py` (`ImuConfig`, `GpsConfig`) |
| Frame/gravity resolution | `src/preprocessing/imu_frame.py` (new) |
| Decimation | `src/preprocessing/` (new per-tick aggregation step) |
| Live routing | `src/preprocessing/live.py` (extend router) |
| Auto-tagger | `scripts/autotag.py` (new) + daemon hook; writes via `src/collection/tags.py` |
| Segmenter modes | `src/preprocessing/segmenter.py`, `src/config.py::OperatingModeSpec` |
| Grade payoff | `data/behavior_models.yaml`, `src/analysis/refit.py` |

## Implementation checklist

Split by what needs hardware. Phase 2a is fully laptop-doable now
(config default `source: disabled`, synthetic-data tests); Phase 2b is
blocked on purchasing/wiring the IMU + baro + 10 Hz GPS.

### Phase 2a — software slice (no hardware required)

- [ ] **2a.1 — `imu` source plumbing.** Extend the `TelemetrySource`
  Literal in `src/telemetry.py` with `"imu"` + an `.imu()` factory
  mirroring `.obd()`; explicit `imu`/`gps` arms in the
  `src/preprocessing/live.py:102` router instead of the catch-all.
- [ ] **2a.2 — `ImuConfig`.** `src/config.py` block per the config
  sketch (`source: bno055|mpu6050|icm20948|disabled`, default
  `disabled`; i2c/sample-rate/mount fields), same `_Strict` pattern as
  `GpsConfig`. Daemon startup degrades cleanly when disabled.
- [ ] **2a.3 — Decimation step.** Pre-pivot per-resample-tick
  aggregation for high-rate sources: `imu:ACCEL_LON_MAX/RMS`,
  `imu:JERK_MAX`, `imu:ACCEL_LAT_MAX`. Raw rows still land in Parquet;
  only the wide frame is decimated. Synthetic 100 Hz fixture tests.
- [ ] **2a.4 — `imu_frame.py`.** Gravity low-pass gated on quiescent
  windows, GPS-course heading resolution with persisted per-session yaw
  offset, longitudinal sign from the OBD speed derivative. Synthetic
  trajectory tests (known tilt + known maneuvers → recovered frame).
  BNO055 path bypasses steps 1–2 (hardware fusion) but shares sign +
  heading handling.
- [ ] **2a.5 — Offline auto-tagger.** `scripts/autotag.py --session ...`
  writing standard `tags.jsonl` records via
  `src/collection/tags.py::write_tag`, idempotent/re-runnable,
  thresholds in config with the triggering peak in tag fields. The
  GPS-only tags (`stopped`, `highway_cruise`) must work against
  existing stored sessions **now**; IMU-dependent tags
  (`hard_accel`/`hard_brake`/`cornering`/`launch`) are implemented and
  unit-tested against synthetic data, dormant until channels exist.
- [ ] **2a.6 — Live auto-tagger hook.** Same taxonomy evaluated in the
  daemon as thresholds trip, config-gated, off by default until 2b
  validation.
- [ ] **2a.7 — Segmenter accel modes.** `OperatingModeSpec` gains
  `accel_lon_min/max`, `accel_lat_min/max`; `DEFAULT_SIGNAL_COLUMNS` +
  `_required_signals`/`_bounds` arms; example `launch`/`hard_brake`/
  `cornering` mode specs in the sample YAML (inert without IMU columns).
- [ ] **2a.8 — Committee declarations.** Append the future committees
  from `docs/sensor-committees.md` §Roadmap (longitudinal accel,
  heading/yaw, stationary-confirm, altitude, baro-pressure,
  vertical/grade) to `COMMITTEES` in `src/analysis/committees.py` as
  `trainable=False` compute-but-exclude declarations gated on member
  availability, with synthetic-data tests. They activate as hardware
  channels appear, no code change.

### Phase 2b — hardware + validation (blocked until parts arrive)

- [ ] **2b.1 — Hardware.** BNO055 (UART mode — Pi I²C clock-stretching
  caveat), BMP390 baro, 10 Hz GPS with PPS; wiring + `pi-setup.md`
  section. *(operator)*
- [ ] **2b.2 — `imu_collector.py`.** Sensor-rate thread, reconnect
  supervision like `obd_collector.py`, live validation.
- [ ] **2b.3 — PPS time discipline.** chrony/gpsd PPS; verify pre-NTP
  session wall time.
- [ ] **2b.4 — Threshold tuning.** Re-run the offline tagger over real
  drives; tune θ values in config.
- [ ] **2b.5 — Grade payoff.** `derived:GRADE` from baro + GPS speed;
  near-flat training filter or grade context axis; refit and measure
  deviation-band tightening.

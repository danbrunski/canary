# Sensor committees

Cross-checking **redundant sensors that measure the same physical quantity**.
When independent sensors of one quantity disagree, the disagreement itself is a
signal — a sensor is faulting. This is the "committee/ensemble for decisions"
idea applied at the *sensor* level, complementing the detector-level committee
in `alerts/fusion.py` (IF + LSTM + behavior).

## What a committee is — and isn't

There are two different cross-check ideas in this codebase; keep them separate.

| | **Sensor-redundancy committee** | **Physical-relationship residual** |
|---|---|---|
| Inputs | Same quantity, *independent* sensors | *Different* quantities with an expected relationship |
| Residual means | A **sensor** is wrong | The **machine** is misbehaving (sensors fine) |
| Example | OBD VSS vs GPS speed | STFT bank1 vs bank2; MAF/RPM air-mass; TPS-V vs % |
| Lives in | `analysis/committees.py` | `analysis/derived.py` (`compute_anomaly_signals`) |

STFT_1 / STFT_2 are **two banks of the engine**, not two readings of one number —
so bank balance is a physical residual, not a committee. The two are **not**
migrated into each other: a committee answers "do my redundant sensors agree?",
a physical residual answers "is the engine behaving?".

## The abstraction

A `SensorCommittee` (see `src/analysis/committees.py`) declares:

- `members` — wide columns measuring the *same* quantity (≥ 2).
- `gates` — row preconditions (e.g. GPS fix quality); a committee only scores a
  row when **all** gates hold, and a missing gate channel **fails closed**.
- `min_members` — how many members must report on a row to cross-check.
- `trainable` — whether the residual joins the trained feature set.

It emits one `derived:<NAME>` column per tick: the **spread (max − min)** across
present members — `~0` when they agree, large on dissent. The spread form scales
from 2-member pairs to 3-member groups identically.

Design rules:

- **Residual-as-feature.** The divergence is an ordinary `derived:` feature the
  IF/LSTM learn — no separate fusion source. (Decision A; revisit if an explicit
  fusion member is wanted later.)
- **Compute-but-exclude.** `trainable=False` committees are still computed and
  available (dashboards / inspection) but kept out of the trained set via
  `feature_pipeline.candidate_feature_columns` — mirrors `NON_TRAINABLE_GPS_CHANNELS`.
  Used while a member's stored data predates a quality fix.
- **Transforms live upstream.** A member needing a rate or unit conversion is
  precomputed as its own `derived:` column first; a committee only compares
  existing columns.
- **One seam.** Computed in `featurize_wide_frame` (after `compute_anomaly_signals`),
  so offline training and the live daemon get identical residuals.

## Roadmap — 4 sources → ~10 committees

Today: OBD/ECM + GPS. Planned: a **barometric sensor** and an **IMU**.

| Committee | Members | Sources | Status |
|---|---|---|---|
| Speed | `obd:SPEED` ↔ `gps:speed` (↔ IMU ∫aₓ) | OBD+GPS+IMU | **active** (excluded from features for now) |
| Longitudinal accel | IMU aₓ ↔ d/dt(OBD speed) ↔ d/dt(GPS speed) | IMU+OBD+GPS | future |
| Heading / yaw | GPS heading-rate ↔ IMU gyro-Z | GPS+IMU | future |
| Stationary confirm | OBD≈0 & GPS≈0 & IMU still | OBD+GPS+IMU | future (also hardens `idle_transient`) |
| Altitude | `gps:altitude` ↔ baro-altitude | GPS+baro | future |
| Barometric pressure | baro ↔ OBD baro PID (0x33) ↔ MAP@idle | baro+OBD | future |
| Vertical / grade | baro alt-rate ↔ GPS v-speed ↔ IMU ∫a_z | baro+GPS+IMU | future |

Each future committee is **one declaration** appended to `COMMITTEES`. Several
are 3-member — which is what makes a general framework (vs ad-hoc pairs) the
right abstraction rather than over-engineering.

## Related: speed signal priority

The segmenter's `speed` signal is **OBD-primary** (`obd:SPEED` → `gps:speed`
fallback): the wheel-speed PID reads a true 0 the instant the truck stops and
doesn't over-read on a marginal fix (observed GPS 151 km/h vs OBD 110 while
moving). GPS *corroborates and back-stops*; its disagreement with OBD is the
speed committee's residual — not a coalesce winner. GPS speed is additionally
quality-gated at capture (`gps_collector`: ≥7 satellites, HDOP ≤ 2.0) so the
logged channel and the fallback are trustworthy.

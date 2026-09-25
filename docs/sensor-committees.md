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
- `gates` — instrument-quality row preconditions (e.g. GPS fix quality); a
  missing gate channel **fails closed**.
- `min_members` — how many members must report on a row for the spread.

There is no `trainable` flag any more (H04.1).

## Committees are detectors, not features (H04, 2026-09-25)

H00 operator decisions 1 and 2 (2026-09-23): external sensors never train or
score the Isolation Forest, the sequence detector or the behavior models; they
take part in detection **only** through committees. So:

- **No committee channel trains.** `committee_channel_reason` names every
  `derived:<NAME>` spread and every member transform, and
  `src/trainability.py` refuses them by name — even `derived:OBD_SPEED_ACCEL_G`,
  whose inputs are all ECU data. None of the deployed IF/LSTM manifests
  (2026-09-25) names a committee column, so nothing served changes.
- **Relations are profile data.** `data/<profile>/committees.yaml` states, per
  committee, a `reference` member, each other member's `ratio`/`offset`
  against it with the `evidence` that measured it, and a `tolerance`
  (`max_departure`, `sustain_seconds`, `min_verdicts`,
  `max_verdict_gap_seconds`) with the `conditions` it was measured under
  (required — a cruise tolerance says nothing about a launch). A committee
  with no entry is reported `not_configured`, never run on defaults: this
  truck's ECU speed reads ~8 % under GPS, and a default ratio of 1 would fire
  on every drive.
- **The evaluator flags.** `CommitteeEvaluator` runs on every live tick (and
  in replay, which is the live path) over the operating point, and offline
  over a resampled session (`evaluate_committee_frame`), the same code. Each
  tick is `agree`, `disagree` or `no_verdict` with a named reason
  (`not_configured`, `reference_missing`, `member_missing`, `stale` — a member
  that did not arrive this tick is not a same-instant pair —, `gate`,
  `condition`). A run of departed verdicts spanning `sustain_seconds` becomes
  one anomaly event (`source: committee`, `detail.detector:
  committee:<NAME>`), naming both members, their values, the relation and
  the departure; an `agree` ends the episode. `/status` carries the
  per-committee state under `detectors.inference.committees`.
- **The spread residual stays** in the operating frame for the session
  fingerprint (`committee_residual_mean:*`), now in engineering units
  (`decoded:` members are scaled through their binding).

### Why committees keep running under a tuning capture (R03.12)

Operator decision 2026-09-24 keeps tuning captures out of the Isolation Forest
and the sequence detector: both score against a *learned* normal, and a
capture that polls a different channel set measures the profile change, not
the engine. A committee learns nothing. It compares two instruments against a
relation measured once per vehicle, and that relation does not depend on what
else is polled — so, like the plausibility checks, committees run under every
capture profile (`capture_profile_gated: false` in `/status`). The speed
committee has a broadcast member (`decoded:WHEEL_SPEED_6F7_A`) precisely
because the `tuning` profile does not poll the speed PID. This is an agent's
reading by analogy (H04.2, 2026-09-25); the operator may overrule it.

### The speed committee (H04.3)

`obd:SPEED` and `decoded:WHEEL_SPEED_6F7_A` against `gps:speed`, ratio 0.9203
for both, gated on ≥ 7 satellites and HDOP ≤ 2, and measured under GPS speed
≥ 20 km/h and |acceleration| ≤ 0.05 g on *both* the GPS and the ECU
derivative (GPS lags the wheels 3-5 s, so the GPS derivative alone misses a
launch — a tuning capture on 2026-09-22 fired falsely without the ECU one).
Events are role `evidence` by default (`models.roles.committee`) until a
committee has a confirmed real catch; `committees.enabled: false` turns the
evaluator off.
Tolerance 15 % sustained 10 s over ≥ 5 verdicts, gaps ≤ 3 s: the calibration
drives already give zero events at 10 %, and held-out drives (including the
ground-truth drive 2026-08-03T163440 and four tuning captures) give zero. A
25 % VSS drop fires 10-13 s after onset at cruise. Numbers and sessions are in the YAML's
`evidence` fields.

### Longitudinal acceleration members (H04.4)

`derived:OBD_SPEED_ACCEL_G` (from the 100 Hz `0x2D1` speed when present, else
`0x6F7`, else the PID) and `derived:GPS_SPEED_ACCEL_G` are computed upstream
as a 2 s trailing difference over real elapsed time. The ECU one stays
ECU-relative; the relation's ratio carries the ECU/ground factor. The committee
itself is `not_configured` until the IMU member (H02) and a known-answer drive
(H02.6) exist to measure its tolerance.

Design rules:

- **Compute-but-exclude.** Every committee channel is computed and available
  (fingerprint, `/status`, inspection) but never a feature.
- **Transforms live upstream.** A member needing a rate or unit conversion is
  precomputed as its own `derived:` column first
  (`append_committee_member_transforms`); a committee only compares existing
  columns.
- **One seam.** Computed in `featurize_wide_frame` (after
  `compute_anomaly_signals`), so offline and the live daemon see identical
  columns.

## Roadmap — 4 sources → ~10 committees

Today: OBD/ECM + GPS. Planned: a **barometric sensor** and an **IMU**.

| Committee | Members | Sources | Status |
|---|---|---|---|
| Speed | `obd:SPEED` / `decoded:WHEEL_SPEED_6F7_A` ↔ `gps:speed` (↔ IMU ∫aₓ) | OBD+GPS+IMU | **flags** (H04.3) |
| Longitudinal accel | IMU aₓ ↔ d/dt(OBD speed) ↔ d/dt(GPS speed) | IMU+OBD+GPS | members computed (H04.4); relation awaits IMU (H02) |
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
speed committee's flag — not a coalesce winner. GPS speed is additionally
quality-gated at capture (`gps_collector`: ≥7 satellites, HDOP ≤ 2.0) so the
logged channel and the fallback are trustworthy.

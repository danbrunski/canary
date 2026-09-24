# A06.3 — naturally aspirated retrain on the current calibration, evaluated (2026-09-23)

The operator's A06.3 decision (2026-09-23): a full naturally aspirated retrain
of the Isolation Forest and the sequence detector, with the A02–A04 fixes and
600 trees, on drives recorded under the ZS44A-20260920 calibration only. This
report covers that retrain and a held-out comparison with the deployed
2026-09-20 artifacts. The raw numbers are in
[`2026-09-23-a06-3-retrain-evaluation.json`](2026-09-23-a06-3-retrain-evaluation.json).

**Nothing was deployed.** The candidates are in `models/_candidates/2026-09-23-na/`,
which is gitignored and outside every served path.

## Result in one paragraph

On the one physical drive neither artifact saw (1.06 engine-hours, 4,825
rows), the candidate sequence model's **global score** is much quieter than
the deployed one's. On the live sessions of that drive it raised 0 episodes
against 29, and it caught a 10 % bank imbalance in 4 of 6 injected windows,
where the deployed model's own flood masked every window. Its per-channel
(family) path still floods at 55 false-alarm episodes per engine-hour. The
candidate **Isolation Forest is worse than the deployed one**. It serves only
city, idle_transient and warmup: idle and highway were refused with 2
training drives against a floor of 3. It withholds 82–91 % of the evaluation
rows, against 43–72 % for the deployed IF. Neither IF raised a false alarm,
and neither caught any injected fault. All of this rests on **one drive**, so
the only intervals are Poisson intervals on episode counts. No drive
bootstrap is possible.

## Corpus

- **Boundary.** ZS44A-20260920 `flashed_at` 2026-09-21T03:08:45.954035Z,
  resolved by `src.models.evaluation.era_resolver` over
  `data/nissan-xterra-2008/tune_revisions.yaml`. The corpus is every session
  starting at or after it in `~/canary-data/sessions/nissan-xterra-2008`, the
  only source used: 53 directories, of which 40 are live sessions and 13 are
  captures. The flash-spanning `2026-09-21T025125` is before the boundary.
- **Quarantine as the pipeline defines it** (`classify_sessions.py`, dry run,
  nothing moved). 13 live sessions have no OBD rows: parked CAN+GPS chatter.
  They were left out of the explicit session list.
- **One more exclusion, by judgement.** `2026-09-23T032755` has OBD rows only
  in its last 162 s. From 03:27Z to 14:26Z it is 11 h of parked GPS chatter,
  and its sidecar disposition is `quarantine` (99.6 % `unknown`). Kept in, it
  chains the 09-22 evening and 09-23 day drives into one 17-hour "drive".
- **Sidecars.** All 40 live sessions carry classifier 0.3.0 sidecars under
  `canary.example.yaml`'s taxonomy. 36 were already current, written minutes
  earlier by the concurrent behavior-refit session. `label_sessions.py`
  labelled the other 4 (`145645`, `150658`, `162918`, `174610`).
- **Captures** never train, because the loader excludes them. Here they only
  bridge drive grouping (see "Code fixes") and are scored as part of their
  evaluation drive.
- **Physical drives** (grouping bridged by captures): D1 09-21 14:21, D2 09-21
  19:42, D3 09-21 22:35, D4 09-22 16:34, D5 09-22 23:38, D6 09-23 14:29.

**Split.** 6 drives cannot carry 3 training, 3 calibration and ≥1 evaluation
drive. `min_training_drives_per_mode = 3` has no override. The automatic
pickers would leave training with one drive (D4), and every IF mode would be
refused. The split used:

| side | drives | sessions |
|---|---|---|
| evaluation (A02.1: newest) | D6 | `145645`, `150658`, `162918` (+ captures `142926`, `164007` when scored) |
| calibration (the calibration picker's rule, 2 drives) | D5, D3 | `233855`, `234813`, `001344`, `002340`, `003917`, `022631`, `022847`; `223515`, `225511`, `231724` |
| training | D1, D2, D4 | `142126`, `142900`, `143331`, `194247`, `195228`, `195641`, `195819`, `201836`, `163415`, `170423`, `170433`, `170453`, `171544` |

`--accept-thin-calibration "operator chose current-calibration-only corpus
(post ZS44A-20260920), 2026-09-23"` is stamped in both manifests.

## Candidate artifacts

| artifact | sha256 | model_version |
|---|---|---|
| `models/_candidates/2026-09-23-na/isolation_forest/model.joblib` | `69463c07bdf11e3a106522cd353e0dea15e60b63c8b6a8b807894b879c952b3c` | `isolation_forest_2026-09-24T003716.963423Z` |
| `…/isolation_forest/model.manifest.json` | `b25bd8607eb48c4f0ad9693ec2078fa8d015873f050d8fd7f72264a2ddb10ac8` | |
| `models/_candidates/2026-09-23-na/lstm_autoencoder/model.pt` | `3edfd8fe056139ddddc6578e6ea3fe1b78668b0b0a9fbac48b5a03216b9c99f2` | `lstm_autoencoder_2026-09-24T003230.943382Z` |
| `…/lstm_autoencoder/model.manifest.json` | `9c9935984c83918bd38f7e17161d6a0787505c20ef4f87c449d9d0a9780bb498` | |

Deployed laptop copies compared against: IF `af1720e7…0e1d`, LSTM
`c77165ad…1112`. These are the A02.11 hashes; they were not checked against
the Pi.

**Configs.** Training used `canary.example.yaml`, the config the deployed
artifacts and A02.11 used. `canary.dev.example.yaml` disables the LSTM
(retrain would skip it) and lacks the koeo/warmup/idle_transient modes the Pi
serves. `models.sequence_detector` is `lstm` in both example configs, and the
feature set is `all_channels`. The harness ran on a copy with
`models.roles: alert/alert`, only so that FA/eh is computed as A02.11 computed
it. Production roles stay `evidence`.

**Duration.** The first run (IF, plus the sequence failure below) took 28 min
wall, 21 min of it the uncached feature-frame build. The IF refit took 1 min.
The LSTM on CUDA ran 27 epochs, early-stopped at best epoch 17, and saved
11 min after its run started. The configured epochs were not reduced.

### Isolation Forest (600 trees)

- **Features and rows.** 204 features (deployed: 295) over 11,084 input rows,
  of which 875 are complete rows.
- **Modes served:** city (147 features), idle_transient (190) and warmup (191).
- **Refused:** highway and idle (`too_few_training_drives`, 2 of 3), koeo
  (17 fit rows), unknown (unclassified).

| mode | fit rows | calibration rows | training drives* | calibration drives | flag rate | episodes | seed_agreement mean / min Jaccard |
|---|---|---|---|---|---|---|---|
| city | 561 | 509 | 5 | 1 | 0.049 | 7 | 0.57 / 0.47 |
| idle_transient | 116 | 14 | 4 | 1 | 0.071 | 1 | 1.00 / 1.00 (one row flagged) |
| warmup | 198 | 111 | 3 | 2 | 0.054 | 2 | 0.68 / 0.50 |

\* `_drive_count` counts drives from the mode's own rows, so a physical drive
whose mode rows sit either side of a capture counts twice. Physically, city
trained on D1, D2 and D4.

**Seed agreement at 600 trees, measured.** City reaches 0.57 here, against
the A04.10 sweep's 0.79 at 500 trees on the 2026-09-20 corpus. The fit sets
are 561 and 198 rows, far below the sweep's 1,048/1,083. idle_transient's
1.00 is one flagged row out of 14.

### Sequence detector (LSTM, `all_channels`)

- **Inputs.** 250 input columns (deployed: 361). Mask rule `arrived_sample`
  (A03.1).
- **Windows.** 1,473 training and 677 validation windows, from validation
  drives chosen by rule `mode_coverage_by_drive_v1`.
- **Global threshold** 1.0575 (deployed 1.2133), over 431 independent blocks.
- **`training_envelope`** (engine hours per mode, baro/IAT/speed): city
  0.258 h, 80–82 kPa, 19–29 °C, 0–84 km/h. highway 0.160 h, 80–82 kPa,
  72–116 km/h. idle 0.138 h. warmup 0.134 h. idle_transient 0.036 h.
  koeo 0.002 h. unknown 1.363 h. **About 0.73 engine-hours of moded data in
  total.**
- **`suppress_channels`:** configured `[]`, excluded `[]`.
- **`unobserved_in_training`** (new, see "Code fixes"):
  `obd:mem:ECM_ENGINE_SPEED_RAM`, `obd:mem:ECM_IGNITION_TIMING_RAM` and their
  `__mean`.
- **`uncalibrated_channels`:** 9 `__std` columns never observed in
  calibration (`did:ECM_*` ×8, `derived:AFR_BANK_BALANCE__std`).
- **Override violations stamped:**
  - highway, idle_transient and warmup have fewer than 10 calibration windows;
  - IAT and speed coverage is below 80 % in city, highway, idle_transient,
    warmup and unknown;
  - the 9 never-observed channels above.

## Held-out comparison (same drive, same rows, per drive)

Both evaluations score one physical drive, D6, the same rows for every
artifact.
- **"Drive"** is all 5 of its sessions: 4,825 rows, 1.057 engine-h. 0.66 h of
  that is two tuning captures.
- **"Live"** is its 3 live sessions: 2,396 rows, 0.397 engine-h.

The deployed artifacts trained only before the flash. The harness therefore
marks D6 cross-era when both deployed artifacts are loaded, and grades no
injection on it. The deployed detectors were also scored beside a candidate
(IF=deployed with seq=candidate, and the reverse), so they get injections on
the same windows.

**Consistency checks:**
- injection windows are identical across all runs;
- each artifact's per-drive and per-injection results are identical in every
  run it appears in;
- batched and served sequence scoring agree: 0 of 171 windows differ, maximum
  score difference 2.5e-7.

Poisson 95 % intervals are on the episode count. They assume independent
episodes; they are **not** a drive bootstrap, which one drive cannot give.

### False alarms per engine-hour, withheld share, flagged rows

| detector | artifact | Drive: FA/eh (episodes) [Poisson 95 %] | flagged / scored rows | withheld | Live: FA/eh (episodes) [Poisson 95 %] | flagged / scored rows | withheld |
|---|---|---|---|---|---|---|---|
| isolation_forest | deployed | 0.0 (0) [0, 3.5] | 0 / 1,368 | 71.7 % | 0.0 (0) [0, 9.3] | 0 / 1,368 | 42.9 % |
| isolation_forest | candidate | 0.0 (0) [0, 3.5] | 0 / 443 | 90.8 % | 0.0 (0) [0, 9.3] | 0 / 443 | 81.5 % |
| sequence (as served, family path) | deployed | 5.7 (6) [2.1, 12.4] | 3,802 / 4,640 | 3.8 % | 10.1 (4) [2.8, 25.8] | 1,431 / 2,269 | 5.3 % |
| sequence (as served, family path) | candidate | 54.9 (58) [41.7, 70.9] | 1,697 / 4,640 | 3.8 % | 37.8 (15) [21.2, 62.3] | 162 / 2,269 | 5.3 % |
| sequence_global | deployed | 59.6 (63) [45.8, 76.2] | 2,183 / 4,640 | 3.8 % | 73.1 (29) [48.9, 104.9] | 408 / 2,269 | 5.3 % |
| sequence_global | candidate | 20.8 (22) [13.0, 31.5] | 359 / 4,640 | 3.8 % | 0.0 (0) [0, 9.3] | 0 / 2,269 | 5.3 % |
| fused | candidate | 0.0 (0) | 0 | 3.5 % | 0.0 (0) | 0 | 4.6 % |
| plausibility (A05) | — | 0.0 (0) | 0 | 0 % | 0.0 (0) | 0 | 0 % |

**Read the episode count with the flagged-row count.** The deployed sequence
model's per-channel path flags 82 % of scored rows on the drive: one flood,
counted as few episodes. The candidate's more numerous episodes cover 37 %
of rows on the drive and 7 % on the live sessions. By difference (episodes
add per session), the tuning captures carry:
- the candidate's family path: 43 of 58 episodes;
- the candidate's global path: all 22 episodes;
- the deployed global path: 34 of 63 episodes.

The training corpus excludes captures by design, so the capture polling mix
is out of distribution for every sequence model.

### Recall against severity (graded / detected; masked = already alerting on the clean rows)

**Drive.** All 96 injections land in `_capture-tuning-2026-09-23T142926`, the
drive's longest engine-running session.
- Both IFs are **withheld** in every window: the capture does not poll their
  Mode 01 features.
- Both per-channel sequence paths are **masked** in every window.
- The candidate global path detected 2/2 graded windows at every severity of
  maf_gain and lazy_o2 (4 masked each), and was masked on thermostat and
  bank_imbalance.
- The deployed global path was masked in every window.

Two graded windows, including a 2 s O2 lag, are weak evidence. They sit
where the global score is already near its threshold.

**Live** (the injections land in `2026-09-23T150658`):

| fault | severity | IF deployed | IF candidate | seq-global deployed | seq-global candidate | seq (family) deployed | seq (family) candidate |
|---|---|---|---|---|---|---|---|
| maf_gain | 0.90–0.97 | 0/6 each | 0/6 each | masked 6/6 | 0/6 each | masked | masked |
| lazy_o2 | 2–20 s | 0/6 each | 0/6 each | masked 6/6 | 0/6 each | masked | masked |
| bank_imbalance | 2 / 4 / 6 / 10 % | 0/6 each | 0/6 each | masked 6/6 | 0, 0, 0, **4/6** | masked | masked |
| thermostat | 3 / 6 / 10 / 15 °C | 0/3 (3 withheld) | 0/3 (3 withheld) | 0/3 (3 masked) | 0, **1/6**, 1/6, 1/6 | 0,0,0, **2/2** (4 masked) | 0/2 (4 masked) |
| idle_air_leak | all | skipped: no 240 s stretch meets the idle condition on D6 | | | | | |

VUS-PR (threshold-free, no-skill 0.50) on the live sessions:

| fault | IF deployed | IF candidate | sequence deployed | sequence candidate |
|---|---|---|---|---|
| bank_imbalance | 0.59 | 0.58 | 0.55 | 0.76 |
| maf_gain | 0.68 | 0.52 | 0.64 | 0.69 |
| lazy_o2 | 0.60 | 0.50 | 0.59 | 0.59 |
| thermostat | 0.55 | 0.51 | 0.51 | 0.47 |

### Single-quantity shift (A04.1 known answer, `150658`)

- **IF, both artifacts: fail.** A +10σ shift flags under 50 % for every
  quantity (airflow, load, STFT 1/2, A/F, coolant). A04.1's blindness is
  unchanged at 600 trees.
- **Sequence family path, both artifacts: fail on coolant only.**
- **Sequence global:** candidate fails on 6 quantities; deployed fails on
  coolant and STFT_1.
- **Fused:** invalid (the control reached 45 %).

## Code fixes made during the retrain (each with a default-tier test)

1. **Captures split physical drives in every retrain holdout.**
   `src.models.evaluation.drives_of_sessions` grouped only the sessions it
   was given. A training pool never holds captures, so live sessions either
   side of a 20-minute capture read as two drives. The automatic split put
   D2's first half on the evaluation side and its second half (`201836`) in
   training. Captures beside the sessions now bridge the grouping and are
   dropped from the result.
   Test: `tests/test_retrain_script.py::test_a_capture_window_keeps_the_sessions_either_side_one_drive`.
2. **The IF coverage floor counted rows the IF can never score.**
   `isolation_forest_feature_columns` measured the 50 % floor over every
   row. 72 % of this corpus is `unknown` mode (engine-off GPS chatter), so
   every engine channel fell under it. The first candidate IF kept 8 columns
   (`gps:speed` and `derived:AFR_CENSORED_B1` variants). Its warmup model
   scored every calibration row identically: threshold −0.0, flag rate 1.0.
   The floor is now measured over classified-mode rows
   (`isolation_forest_coverage_rows`); an all-unknown frame is measured
   whole. That first artifact was discarded.
   Test: `tests/test_feature_pipeline.py::test_if_coverage_floor_ignores_rows_the_if_can_never_score`.
3. **A channel seen only in the validation drives crashed the sequence
   trainer.** A03.4 fits the scaler on training rows only.
   `obd:mem:ECM_ENGINE_SPEED_RAM` existed only in a validation session, and
   `ZScoreScaler.fit` raised. `train_sequence.train` now drops such columns
   before the feature set is resolved, and records them in the schema under
   `unobserved_in_training`.
   Test: `tests/test_train_sequence_columns.py::test_a_column_seen_only_in_validation_is_not_a_feature_and_is_named`.

## What this does not show

- **Recall on a real fault.** There is none to test. All of the above is
  injected.
- **Anything about highway or idle IF behaviour.** The candidate has no model
  for either.
- **Deployed-vs-candidate on A02.11's drives.** Those drives are pre-flash,
  so they are cross-era for the candidate.
- **Pi serve cost of 600 trees.** Not measured.

## r2 — LSTM retrained after one physical-drive definition (2026-09-23, later)

**Why r2 exists.** The verifier found drive grouping implemented in three
places, and only one of them used the capture bridge. Two consequences for
r1:
- The r1 LSTM's validation drives (`143331#segment-002`,
  `201836#segment-001`, `171544`) shared physical drives D1, D2 and D4 with
  its training.
- The r1 IF's idle_transient floor counted "4 training drives" where there
  were 2.

The operator then decided:
- deploy the candidate LSTM only, and keep the deployed IF;
- move the 14 excluded sessions to `_archive/`;
- split a drive wherever vehicle activity pauses inside one recording.

All drive grouping now goes through `src/analysis/drives.py`.
- **Definition.** A drive is vehicle activity: OBD/UDS responses and decoded
  engine broadcast. Raw CAN frames count only for a recording that has
  neither.
- **Bridging.** Sibling recordings, captures included, bridge the grouping.
- **Splitting.** A pause longer than 300 s splits a drive, even inside one
  recording. `2026-09-23T032755` never stopped writing GPS, so a data-gap
  rule could not have split it.
- **Frames.** `build_feature_frame` and `build_evaluation_frame` stamp each
  row's drive (`physical_drive`). All of these read the stamp:
  - the IF per-mode floors;
  - the sequence validation units;
  - the sequence calibration drive count;
  - behavior `group_drives`.
- **Holdouts.** A directory whose activity falls in two drives is one holdout
  unit. The harness groups drives the same way, and its "seen" check strips
  `#segment-00N`.

**Retrain.**
- Same corpus (26 live sessions), same split, same
  `--accept-thin-calibration` reason, same `canary.example.yaml`.
- `--skip-benchmark`: the harness below supersedes it, and that section of
  `retrain.py` is being edited by another session.
- 30.5 min wall on a heavily loaded host (load average 20–60 from concurrent
  sessions). LSTM on CUDA, 34 epochs, best 24.

| artifact | sha256 | model_version |
|---|---|---|
| `models/_candidates/2026-09-23-na-r2/lstm_autoencoder/model.pt` | `f7389acfee862bc842f0e19e1f72293288f123e6912f178b2396063a17461f7a` | `lstm_autoencoder_2026-09-24T035125.284392Z` |
| `…/lstm_autoencoder/model.manifest.json` | `cc034239f78b6b57623f676f43e4a6d305fd68ab51393f511546c4486b68c6b6` | |
| `models/_candidates/2026-09-23-na-r2/isolation_forest/model.joblib` (record only, not for deploy) | `6c21a649ae44a78b0632d0eb19cf2e260318b50f36f9880cf342e2f4cc0ca9dd` | `isolation_forest_2026-09-24T035020.805762Z` |
| `…/isolation_forest/model.manifest.json` | `acb54e08fa9161f8e6f26445117f3e0da11d31a2ee70d6bb005d0e9a7fb48327` | |

**r2 LSTM manifest**
- Rule `mode_coverage_by_drive_v2`. 254 input columns.
- Windows: 1,479 training and 671 validation.
- Global threshold 1.1647, over 431 independent blocks.
- `suppress_channels` is empty.
- Validation is one unit, `2026-09-21T201836#segment-002`. It has no physical
  drive: its rows are the parked stretch after 20:21Z, with GPS drift up to
  8 km/h labelled `city`. So validation shares no drive with training, but the
  early-stopping signal comes from parked rows (see "Open").
- `unobserved_in_training` is empty. The two `obd:mem:` columns trained this
  time and are uncalibrated instead: 13 uncalibrated channels in all.
- `training_envelope`: city 0.301 h, highway 0.160 h, idle 0.181 h, warmup
  0.134 h, idle_transient 0.039 h, unknown 1.296 h.

**r2 IF (record only).**
- Serves city (120 features, 3 training drives) and warmup (158 features).
- Refused:
  - idle: 1 physical drive;
  - highway: 2 drives;
  - idle_transient: 37 fit rows after quarantine.
- Seed agreement: city 0.60, warmup 0.60.

**Correction to r1's IF table.** The r1 IF above came from an IF-only
re-run (`--skip-sequence`). `retrain.py` builds the shared frame, and
therefore applies quarantine exclusion, only when both trainers run. So r1's
IF trained without quarantine exclusion: `lost_to_quarantine` 0. r2 trained
both detectors together and lost 369 of 709 city rows and 64 of 199 warmup
rows to quarantine.

### r2 evaluation (D6, same rows and same injection windows for both LSTMs)

Each LSTM is scored beside an IF from the other set, so the harness grades
injections:
- deployed IF + r2 LSTM;
- r2 IF + deployed LSTM.

Both runs share one grouping (`drive-2026-09-23T142926`, sibling captures
bridging), the same rows and the same injection windows. The windows differ
from r1's: the injection seed includes the drive id, and bridging moved the
drive's start. Batched/served parity: 0 of 171 windows differ.

**Clean false alarms per engine-hour** (episodes; Poisson 95 %; flagged /
scored rows):

| set | detector | deployed LSTM | r1 LSTM | **r2 LSTM** |
|---|---|---|---|---|
| live (2,396 rows, 0.397 eh) | family path (as served) | 10.1 (4) [2.8, 25.8]; 1,431 / 2,269 | 37.8 (15) [21.2, 62.3]; 162 / 2,269 | **20.2 (8) [8.7, 39.7]; 131 / 2,269** |
| live | global | 73.1 (29) [48.9, 104.9]; 408 | 0.0 (0); 0 | **0.0 (0) [0, 9.3]; 1** |
| drive incl. captures (4,825 rows, 1.057 eh) | family path | 5.7 (6) [2.1, 12.4]; 3,802 / 4,640 | 54.9 (58); 1,697 | **30.3 (32) [20.7, 42.7]; 2,059 / 4,640** |
| drive incl. captures | global | 59.6 (63) [45.8, 76.2]; 2,183 | 20.8 (22); 359 | **32.2 (34) [22.3, 44.9]; 1,170** |

- Withheld: 5.3 % of rows on the live set and 3.8 % on the drive set, for
  every LSTM.
- Deployed IF: 0 false alarms, withheld 42.9 % (live) and 71.7 % (drive).
- The deployed family path's low episode count is one long flood: 63 % of live
  rows flagged, 82 % of drive rows.
- r2's episodes on the drive set come mostly from the two tuning captures. By
  difference against the live set: 24 of 32 family, 34 of 34 global.
- Fused (deployed IF + r2 LSTM): 0 on both sets. Plausibility: 0.

**Recall** (live set; detected / graded; *m* = masked, i.e. already alerting
on the clean window; drive-set injections all land in a capture where every
LSTM path is masked):

| fault | r2 global | deployed global | r2 family | deployed family | deployed IF | r2 IF |
|---|---|---|---|---|---|---|
| maf_gain 0.90–0.97 | 0/6 each | m 6/6 | m 6/6 | m 6/6 | 0/6 each | 0/6 each |
| lazy_o2 2–20 s | 0/6 each | m 6/6 | m 6/6 | m 6/6 | 0/6 each | 0/6 each |
| bank_imbalance 2–10 % | 0/6 each | m 6/6 | m 6/6 | m 6/6 | 0/6 each | 10 %: 1/6 |
| thermostat 3 / 6 / 10 / 15 °C | 0, 0, **1/6**, **2/6** | 0/1 (m 5) | 0/3 (m 3) | 0/1 (m 5) | 0/5 (1 withheld) | 0/5 (1 withheld) |

VUS-PR on the live set (no skill 0.50):

| fault | r2 LSTM | deployed LSTM |
|---|---|---|
| bank_imbalance | 0.66 | 0.62 |
| maf_gain | 0.61 | 0.60 |
| lazy_o2 | 0.53 | 0.58 |
| thermostat | 0.53 | 0.55 |

Deployed IF VUS-PR is 0.67 / 0.64 / 0.57 / 0.54 against r2 IF 0.54 / 0.49 /
0.49 / 0.50, in the same order.

**Single-quantity shift:**
- r2 family path: fails on coolant only on the live session. It **passes** on
  the drive set's capture session, the first pass recorded for any detector.
- Deployed family path: withheld on the capture (nothing scored quiet).
- Both IFs fail or are withheld.

### What r2 changes in the r1 reading

- r1's bank-imbalance catch (4 of 6) is not reproduced. It was on different
  windows, and r2's global path is 0 of 6 on its own windows.
- r2's global score is as quiet as r1's on live rows (0 episodes).
- r2's family path is about half r1's: 8 against 15 episodes live, 32 against
  58 with captures.
- Against the deployed LSTM, r2 flags about a tenth of the live rows. The
  deployed LSTM runs a near-continuous flood that masks every injected window.

### Open

- **Validation rests on parked rows.** Pieces with no vehicle activity (parked
  GPS rows the segmenter labels `city` from GPS drift) still enter sequence
  training and validation. Whether they belong in the sequence corpus is not
  decided here.
- **Deployment** of the r2 LSTM (the operator chose LSTM-only) is the
  orchestrator's. It should serve beside the deployed IF.

## r3: parked rows dropped from sequence training and validation (2026-09-24)

**What changed.** The operator took the recommended option (b): rows with no
physical drive neither train nor validate.

- **Parked-row filter.** `scripts/train_sequence.py::train`, the one entry
  every sequence retrain goes through, drops rows whose `physical_drive` is
  null (`src.analysis.drives.drop_rows_without_drive`). The count is recorded
  as `schema.rows_without_physical_drive`.
- **IF-only quarantine gap fixed.** `retrain.py` now builds the shared frame,
  and so applies quarantine exclusion, whichever trainers run.
- **Latent bug found and fixed.** The first r3 attempt refused the LSTM. With
  the parked rows gone, no training drive could be held out without leaving a
  mode untrainable, so validation fell back to A03.5's stated tail block. The
  scaler had been fit on every row, including that tail, so the A03.4 leak
  check refused.
  - The scaler is now fit on the rows before the tail
    (`sequence_training.tail_block_validation_start`).
  - The library check compares each validation window's start with the last
    row the scaler read from that session (`scaler_fit_until`).

**Retrain.**
- Same 26 sessions, split, `canary.example.yaml`, override reason and
  `--skip-benchmark`.
- 10.9 min wall.
- 6,869 of 11,084 training-frame rows were parked and dropped.
- Training windows fell from 1,479 (r2) to 600; validation has 158 windows.
- Best epoch 47, stopped at 57. Global threshold 1.3115.

| artifact | sha256 | model_version |
|---|---|---|
| `models/_candidates/2026-09-23-na-r3/lstm_autoencoder/model.pt` | `b14fc5993f1b984ddd7d91b868600652a51c2f88aa5f11ad6409dee05ed6ec65` | `lstm_autoencoder_2026-09-24T070311.255643Z` |
| `…/lstm_autoencoder/model.manifest.json` | `a2764fc0516ca7dcce10ffeaa19f8325d3acbf499d9d53df290f3dff029ff76a` | |
| `models/_candidates/2026-09-23-na-r3/isolation_forest/model.joblib` (record only) | `0e3011a24ad221ffdec8e5d7d58900e0d6b4a8e38c75bc2031189dae8b040c86` | |
| `…/isolation_forest/model.manifest.json` | `aeb80b637d190f609be965604506aeb08fc893b54474d9f889665f7ecc4d7d42` | |

**Is r3's validation real driving? Yes, but it is not an independent drive.**
- It is the tail block of D4 (`drive-2026-09-22T165340`), 16:58:15Z to
  17:23:39Z across 163415, 170423, 170433, 170453 and 171544.
- OBD speed averages 43.9 km/h, maximum 106 km/h.
- Rows by mode: city 429, highway 81, idle_transient 59, idle 9, koeo 6, and
  unknown 296 (engine running, unclassified).
- The rest of D4 trains. The tail is separated from training by a gap of 30
  windows, and the manifest records `fallback: tail_block`.
- `scaler_fit_excludes_validation` is `true`.

### r3 on D6, beside the deployed IF (same rows and injection windows as r2)

Batched and served scoring differ on 0 of 171 windows.

| set / path | deployed LSTM | r2 LSTM | **r3 LSTM** |
|---|---|---|---|
| live: per-channel, episodes (FA/eh [Poisson 95 %]); flagged / 2,269 | 4 (10.1); 1,431 | 8 (20.2 [8.7, 39.7]); 131 | **15 (37.8 [21.2, 62.3]); 147** |
| live: global | 29 (73.1); 408 | 0; 1 | **1 (2.5 [0.1, 14.0]); 6** |
| with captures: per-channel; flagged / 4,640 | 6 (5.7); 3,802 | 32 (30.3 [20.7, 42.7]); 2,059 | **70 (66.2 [51.6, 83.7]); 1,650** |
| with captures: global | 63 (59.6); 2,183 | 34 (32.2 [22.3, 44.9]); 1,170 | **37 (35.0 [24.6, 48.2]); 956** |

- **Recall, global path, live set:** thermostat 15 °C detected 1 of 3 graded
  (3 masked). r2 had 10 °C at 1 of 6 and 15 °C at 2 of 6, none masked. r3's
  global path also masks 2–3 of the 6 windows for every other fault. The
  per-channel path stays masked for both models.
- **VUS-PR, live set:** bank_imbalance 0.65, maf_gain 0.59, lazy_o2 0.51,
  thermostat 0.49. r2 had 0.66, 0.61, 0.53 and 0.53.
- **Single-quantity shift:** r3 fails on both sets (coolant, plus four more
  quantities on the global path). r2's per-channel path passed on the capture
  session.
- **Deployed IF:** 0 false alarms, 42.9 % (live) and 71.6 % (with captures)
  of rows withheld.

### Recommendation: deploy r2, not r3; keep the parked-row filter

On identical D6 rows and windows, r3 is noisier than r2 and no better at
detection:

- **Noisier:**
  - per-channel episodes nearly double (15 vs 8 live; 70 vs 32 with
    captures, where the Poisson intervals do not overlap);
  - the global path goes from 0 to 1 on live driving;
  - r3 masks injected windows that r2 grades.
- **Why:**
  - dropping parked rows cut training to 600 windows, 41 % of r2's 1,479;
  - the remaining three drives cannot spare one for validation, so r3
    early-stops on the tail of a training drive (158 windows).
- **What r2 does better:** the cleaner validation does not show up as better
  held-out behaviour. r2's weakness, early stopping judged on a parked tail,
  costs less on this evidence than r3's loss of training data.
- **Keep the filter:** the rule stays in the training path. Once the corpus
  has more drives, a retrain will hold out a whole real drive. Deploying r2
  means the served artifact predates the rule; its manifest does not record
  `rows_without_physical_drive`.
- **Caveat:** this is one evaluation drive. The live per-channel intervals
  overlap.

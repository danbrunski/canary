# B08.1 — artifact input lineage after the CAN DID corrections (2026-10-05)

[B08.1](../../docs/roadmap/tasks/B08-firmware-informed-behavior.md). Full
machine output: [JSON](2026-10-05-b08-1-artifact-lineage.json), produced by

```
.venv/bin/python tools/audit_artifact_lineage.py --extra-models ~/canary-data/models-0920a \
  --sessions-root ~/canary-data/sessions/nissan-xterra-2008 --sessions-root data/sessions \
  --sessions-root ~/canary-data \
  --negative-control-session ~/canary-data/sessions/nissan-xterra-2008/_capture-rom-trace-2026-09-21T142926 \
  --out data/analyses/2026-10-05-b08-1-artifact-lineage.json
```

at `71a7317` plus the working tree. `data/dids.yaml` carried another
session's uncommitted `0xFFFF2842` rename, which the ledger records as
`working-tree`. The audit ran offline and the Pi was not contacted. The
deployed set is therefore taken from `tests/fixtures/deployed_manifests` and
the tracked behavior artifact.

Revision 2 (same day). An independent verification found six gaps in the first
run, and this revision closes them:

1. Trim claims are now checked for term *and* bank.
2. RAM channels get the as-recorded check, and their stored payloads are
   re-decoded.
3. Derived inputs come from the build-time source, and a definition that
   cannot be located is not counted.
4. Mode 01 names resolve through the collector's registry.
5. The Mode 01 recheck is de-duplicated per artifact file.
6. `agreement_counts_as` is validated against an enum.

Several artifacts moved from unaffected to unverifiable as a result.

## Method

A name is only a label on a catalog entry, and `c897a62` (2026-10-04) moved
labels. `ECM_LONG_FUEL_TRIM_B1` read DID `0x1102`, which is road speed on CAN;
it now reads `0x1125`. `ECM_LONG_FUEL_TRIM_B2` read `0x1125`, the bank-**1**
word; it now reads `0x1126`. For that reason no input is judged by its name.

1. **Inputs.**
   - Behavior models: every channel in the recipe (output, axes,
     conditions, eligibility), not the fit.
   - IF/sequence manifests: every `feature_columns` and
     `behavior_model_columns` entry, reduced to its channel.
2. **Derived dependencies come from the build-time source.**
   - The derived module committed before the build instant (`git`;
     `src/analysis/derived.py` before R09.4) is *parsed*, never imported.
   - Three literal declaration forms are recognised:
     - `DERIVED_CHANNEL_INPUTS` dict entries;
     - `DerivedSignal(name, groups, …)`;
     - `_append_dependency_gap(out, name, *groups)`.
   - A channel declared any other way is **definition_unlocated** and counts
     as unverifiable, never as unchanged. The other forms are vehicle modules,
     comprehensions, `afr.py`, and trip channels computed from local
     variables.
   - A located definition that differs from today's
     `DERIVED_CHANNEL_INPUTS` is **definition_changed**, which counts as
     affected.
3. **The catalog at build** is `dids.yaml` + `bindings.yaml` at the last
   commit before `fit.fitted_at` / `created_at`, compared against the
   working tree.
   - Stored-name migrations in `tools/channel_renames/*.yaml` are followed.
     They rewrote names inside artifacts without moving any bytes.
4. **Source identity**, resolved per leaf:
   - Mode 01 PID, through `obd_registry.MODE01_REGISTRY` with `PID_DECODERS`
     as fallback;
   - the OBD service, for `MIL_STATUS`, `DTC_*` and Mode 06 counts;
   - DID number;
   - RAM address;
   - CAN id plus byte slice.

   A changed identity, a changed *effective* decode, or a retired entry makes
   the input **affected**. The effective decode follows `linear_factors`: a
   null offset is zero, and a null scale yields the raw count.
5. **As-recorded values.**
   - Every reader decodes DID replies on read, and broadcast rows since P02.8.
   - RAM values (`mem:`) are stored decoded at record time at **every** build
     date. So are broadcast rows before P02.8.
   - For those inputs the audit requires that a single decode was committed
     across `[first training session, build]`.
     - If the decode varied within that window, the input is
       **as_recorded_varied**.
     - If the artifact names no dated session, it is **as_recorded_unknown**.
     - Both count as unverifiable.
6. **Trim qualification uses register and claim.**
   - [`observation_aliases.yaml`](../nissan-xterra-2008/observation_aliases.yaml)
     declares the shared registers, taken from the firmware pointer tables
     and the PID handlers.
   - It also declares `trim_claims`, the quantity each trim-named channel
     claims to be.
   - A leaf with a claim qualifies only when its source identity reads a
     register holding exactly that quantity: same term (short/long), same
     bank. A test requires every trim-named Mode 01 PID and CAN DID to
     declare a claim.
7. **Re-decode.** For every artifact's fit, calibration and training sessions
   found on this laptop:
   - stored Mode 01 values are compared with the registry decoder applied to
     the stored payload;
   - raw `DID_<hex>` replies of the artifact's DIDs and of the five corrected
     DIDs are decoded with the build-time entry and with the current entry;
   - stored RAM rows get two decodes:
     - first, the offset in the stored window reply is found at which the
       catalog in force at recording reproduces the stored value;
     - then the same bytes are decoded with the current entry.

   After that, every stored session (284 directories) is scanned for raw
   corrected-DID replies, and for sidecars whose stored name now reads a
   different DID.

## Result: 7 affected, 65 unaffected, 11 unverifiable (83 artifacts)

| Set | Artifacts | Verdict |
|---|---|---|
| Served behavior set `behavior_models.yaml` (sha `816c150b…`) | 12 | 12 unaffected |
| `_prior/2026-09-24T024700` | 8 | 8 unaffected |
| `_candidates/2026-09-23`, `-warm-idle-local-range`, `-warm-idle-steady` | 36 | 36 unaffected |
| `_candidates/2026-09-26-bank-balance-persistence` | 1 | unaffected |
| `tests/fixtures/behavior_validation/behavior_models_2026-09-22.yaml` | 8 | 8 unaffected |
| Deployed manifests (fixture: IF 2026-09-20, LSTM 2026-09-24 na-r2) | 2 | **2 unverifiable** |
| `models/_candidates/2026-09-23-na{,-r2,-r3}` IF + LSTM | 6 | **6 unverifiable** |
| `models/_ablation-a03/all_channels` | 1 | **unverifiable** |
| `models/nissan-xterra-2008/isolation_forest` (09-20) + `~/canary-data/models-0920a` copy | 2 | **2 unverifiable** |
| `models/nissan-xterra-2008/lstm_autoencoder` (09-20) + `~/canary-data/models-0920a` copy | 2 | **2 affected** |
| `models/_archive-unscoped/isolation_forest`, `_overnight-1.6-if`, `_overnight-1.6-lstm`, `_bakeoff/lstm`, `_bakeoff/tcn` | 5 | **5 affected** |

**No artifact consumed a corrected CAN DID.** None of the 83 artifacts reads
DID `0x1102` or `0x1123`–`0x1126`, under any name, at build time or now. No
located derived definition changed between build and now.

### Affected

- **The laptop's 2026-09-20 LSTM** (`models/nissan-xterra-2008/lstm_autoencoder`,
  with an identical copy in `~/canary-data/models-0920a`).
  - `did:ECM_INJECTOR_PULSE_1/2` (DIDs `0x1206/0x1207`) were trained as raw
    counts (`scale: null`) at `9fd6032`.
  - `1156df0` (2026-09-20 21:21Z, 15 h after the build) pinned `scale` to
    0.01 ms, so the same features now arrive 100x smaller.
  - The bytes are the same; only the decode changed. This is not a DID
    identity error, and it is not the deployed model.
  - The derived inputs listed under the deployed IF below are also
    unlocated.
  - No data-level check was possible. Of the LSTM's 30 sessions, 14 are on
    this laptop, and none of those carries raw `0x1206/0x1207` replies.
- **Five archived or research manifests**: `_archive-unscoped` IF 07-20,
  `_overnight-1.6-if/-lstm` 07-19, `_bakeoff` LSTM/TCN 05-26.
  - All five consume two retired bindings:
    - `decoded:ECM_CALC_LOAD_B`, removed by `b374ba4` on 2026-08-06 (the
      aggregate-match retraction);
    - `decoded:ECM_AIRFLOW_PROXY_23D`, removed by `c341570` on 2026-08-05.
  - The bakeoff pair also consumes the `dbc:` forms of both.
  - Their derived inputs (`AIR_MASS_PER_CYCLE`, `STFT/LTFT_BANK_BALANCE`) are
    also unlocated. The derived module of that era computed them through
    local variables and declared no literal inputs.
  - Nothing serves them; they already sit in `_`-prefixed directories.

### Unverifiable: derived definitions not locatable at build

All 11 are IF or sequence manifests. Every identity they *could* trace is
unchanged. Each one consumes derived channels whose build-time inputs are not
declared literally:

| Derived channel | Where it is defined | Manifests |
|---|---|---|
| `derived:AFR_B1`, `derived:AFR_CENSORED_B1` | `src/signals/afr.py` / vehicle-signals comprehension | all 11 |
| `derived:FUEL_RATE_LPH`, `ESTIMATED_POWER_HP`, `ESTIMATED_TORQUE_NM` | trip channels, computed through local variables | 10 (all but `_ablation-a03`) |
| `derived:INSTANT_ECONOMY_L_PER_100KM`, `INSTANT_ECONOMY_MPG` | trip channels | 7 (the 09-24 builds and the deployed LSTM) |

`VE_PERCENT`, `VE_AMBIENT_PERCENT`, `AIR_MASS_PER_CYCLE`, the bank balances
and `TOTAL_TRIM_B1/B2` *were* located at every build from 2026-09-20 on. Each
matches today's declaration. The deployed IF (built at `f7f6d5f` source) and
the deployed LSTM (`07b14c2`) are in this group. Today's code computes their
unlocated derived features from the current definitions. Whether those equal
the build-time ones cannot be shown from literal declarations, so the audit
does not claim it.

### Unaffected: evidence for the trim, speed and RAM paths

- **Mode 01 total trim, checked explicitly.** `total_trim_vs_rpm_load_b1/b2`
  (served v1, every candidate) resolve as follows:
  - `derived:TOTAL_TRIM_B1` → Mode 01 PIDs `0x06` + `0x07`; `B2` → `0x08` +
    `0x09`.
  - The inputs come from the build-time declaration (located at every build).
  - Each leaf qualifies for its *declared* claim, through the register of the
    same term and bank: `0xFFFF26B4` (STFT B1), `0xFFFF26DE` (LTFT B1),
    `0xFFFF26B6` (STFT B2), `0xFFFF26E0` (LTFT B2).
  - `fuel_trim_vs_rpm_load` (PID `0x06`) and `stft_bank_balance_city/highway`
    (PIDs `0x06`/`0x08`) resolve the same way.
- **Re-decoded Mode 01 payloads, once per artifact file over its distinct
  sessions** (`set_mode01_rechecks` in the JSON):
  - Served `behavior_models.yaml`: 75 distinct sessions, 347,612 rows of its
    13 Mode 01 channels. 270,190 carry a payload, and all 270,190 decode to
    the stored value (0 disagree, 0 undecodable).
  - The remaining 77,422 rows are from the python-OBD era and carry no
    payload. Their identity rests on the J1979 name; they cannot be
    re-decoded.
  - The three 09-23 candidate files give the same figures. The persistence
    candidate has 16 sessions, 12,677/12,677 with payload, all agreeing.
  - `_prior` and the review fixture predate stored payloads: 31 sessions,
    56,036 rows, 3 with payload, which agree.
  - The per-model figures inside each artifact record count a session once per
    model that lists it, so they are not summed anywhere.
- **RAM channels.**
  - Three manifests consume RAM channels: the deployed LSTM and the na-r2 and
    na-r3 LSTMs. The channels are `mem:ECM_ENGINE_SPEED_RAM` (`0xFFFF11D8`)
    and `mem:ECM_IGNITION_TIMING_RAM` (`0xFFFF11C7`).
  - Spec level: one decode was committed across each recording window,
    address and decode are unchanged, and the as-recorded check passes.
  - Data level, thin: their training sessions hold only 2 stored rows per
    channel. Both reproduce under the recording catalog and agree under the
    current one.
  - No behavior model consumes a RAM channel.
- **Payload identity beats session membership.**
  - The served `air_charge_vs_rpm_throttle_did` v2 and its three candidate
    copies list both rom-trace captures as calibration sessions.
  - Those captures hold raw `0x1102` and `0x1125` replies (231 each).
  - The recipe's identities are DIDs `0x1208`, `0x120F`, `0x1219` and Mode 01
    RPM. Their 16,812, 15,637 and 16,684 replies re-decode identically under
    the build and current entries.
- **Renamed inputs kept their bytes.** On artifacts fitted before the rename,
  `did:ECM_AIR_CHARGE` resolves through `2026-09-18-fuel-relabel.yaml` to
  `ECM_INJECTOR_PW_B1`, DID `0x1208`.
- **Broadcast decode drift that is not a change.**
  - `ECM_AIRFLOW_233_B2` went `offset: null → 0.0` under `scale: null` at
    `911d0f1`. The decoder yields the raw count either way.
  - `MOTION_SIGNAL_2D1_1` is `superseded_by: VEHICLE_SPEED_2D1`. That is the
    same bytes under another name, recorded as an alias.

## Road-speed and bank-swap negative controls

Trim qualification (`trim_qualification` in the JSON) gives the following:

| Identity | Claimed as | Result |
|---|---|---|
| DID `0x1102` | LTFT B1 (its pre-`c897a62` label) | refused: register `0xFFFF11A8`, road_speed |
| DID `0x1125` | LTFT B2 (its pre-`c897a62` label) | refused: register `0xFFFF26DE` is LTFT **B1** |
| DID `0x1125` | LTFT B1 | qualifies |
| PID `0x07` | LTFT B1 | qualifies |
| PID `0x09` | LTFT B1 | refused: bank 2 |
| PID `0x0D` | LTFT B1 | refused: road_speed |

Data level, on `_capture-rom-trace-2026-09-21T142926` through
`compare_channels` (250 ms pairing):

- `did:ECM_VEHICLE_SPEED_COARSE` (DID `0x1102`, ×2 km/h) against
  `can:WHEEL_SPEED_6F7_A`: n 202, slope 0.9950, offset +0.43 km/h, r² 0.9994,
  `stable`, b−a +0.20 ± 0.85 km/h.
- Against DID `0x121A`: slope 1.0036, r² 0.9995.

That is road speed, 1:1, and it is refused as trim whatever its name. The
committed slice reproduces it in
`tests/test_artifact_lineage.py::test_the_committed_slice_is_road_speed_and_old_bytes_decode_nothing`.

Raw replies re-decoded corpus-wide. Only the two rom-trace captures ever
polled the corrected DIDs:

| Session | DID | Replies | Catalog then | Decoded then | Decoded now |
|---|---|---|---|---|---|
| `_capture-rom-trace-2026-09-21T142926` | `0x1102` | 202 (1 byte) | `5b7a779`: LTFT B1, 2 bytes | 0 (reply too short) | 0–104 km/h |
| same | `0x1125` | 202 | LTFT B2, offset 0 | 100 % (all) | LTFT B1 0 % (all) |
| `_capture-rom-trace-2026-09-21T195647` | `0x1102` | 29 | `7626241`: LTFT B1, 2 bytes | 0 | 36–44 km/h |
| same | `0x1125` | 29 | LTFT B2, offset 0 | 100 % | LTFT B1 0 % |

**PID 0x44 and target RAM.**
- `observation_aliases.yaml` records this pair as
  `agreement_counts_as: decode_verification`. The loader accepts only
  `decode_verification` and `dependent_evidence`.
- PID `0x44` = `0xFFFF26BE` × `0xFFFF46AC` / 3675, so the two agree only
  while the A/F target is 14.7.
- Their agreement verifies the decode of one register; it does not validate
  the target-φ algorithm.
- No audited artifact consumes `mem:ECM_TARGET_EQUIV_RATIO`. The deployed
  LSTM consumes PID `0x44`, whose identity is unchanged.

## Stale labels and findings

- **Session sidecars** that store a name beside a request identity: 8 entries,
  all in the two rom-trace captures.
  - Files: `capture_profile.json` (`resolved_polling_plan`) and
    `capture_quality.json` (`channels`).
  - Request `0x1102` is stored as `ECM_LONG_FUEL_TRIM_B1`, a name that now
    reads `0x1125`.
  - Request `0x1125` is stored as `ECM_LONG_FUEL_TRIM_B2`, a name that now
    reads `0x1126`.
  - Raw rows are keyed by DID number, so decode-on-read is correct; only the
    labels are wrong.
  - Left as recorded.
- **Archived capture profile.** The `rom-trace` profile in
  `capture_profiles.archive.yaml` lists `ECM_LONG_FUEL_TRIM_B1/B2`. Nothing
  loads that file; it is kept verbatim by design.
- **Findings marked superseded.** A note was added to each; nothing was
  deleted.
  - [`2026-09-23-drive-log.md`](2026-09-23-drive-log.md): the advice to read
    "0x1102/0x1125 (the learned table)" on CAN.
  - [`2026-09-20-uprev-usb-road.md`](2026-09-20-uprev-usb-road.md): "0x1125 …
    is bank-2 long fuel trim". That is true on K-line, and the CAN catalog
    took it from this table.
- **Findings read and unaffected:**
  - `2026-09-17-drive-log.md`: 0x1102 listed as uncatalogued.
  - `2026-09-24-abs-kline-scan-tool.md`: the ABS module's own 0x1102–0x1105.
  - `2026-09-24-scan-tool-bcm-ecm-session.md`: DID numbers only.
  - The DID sweep JSONs: `0x1102` by number only.
- **Caches.** No unit in the 67 derived frames under
  `data/cache/derived_sessions` holds a corrected DID. The derived-frame and
  feature-frame caches are keyed by catalog content, so they rebuild when the
  catalog changes.

## Superseding (B03)

No behavior artifact is affected or unverifiable, so no `behavior_policy.yaml`
entry is needed and none was made.

B03's lifecycle policy covers behavior models only. For IF and sequence
manifests, B03 has no mechanism that marks an artifact without deleting it,
beyond `_` directories and producibility, and producibility cannot see a
decode change on a column that is still producible. The affected laptop LSTM
copies and the unverifiable deployed manifests are therefore listed as
operator decisions in the B08.1 checkbox. No retrain, feature replacement or
catalog edit was made.

## Limits

- **Build-time catalog.** "Built" means the artifact's own timestamp, checked
  against *committed* catalogs and derived source. An uncommitted edit on the
  build machine is invisible.
- **As-recorded values.** The audit checks that one decode was committed
  across each recording window. It does not check what the Pi had checked
  out at the time.
- **RAM re-decode.** The window offset is recovered as the one that
  reproduces the stored value under the recording catalog. Rows that no
  offset reproduces are counted as `offset_unresolved`; there were none.
- **Missing payloads.** Python-OBD-era Mode 01 rows carry no payload.
- **Missing sessions.** Of the 2026-09-20 IF and LSTM sessions, 15 of 22 and
  16 of 30 are not on this laptop.
- **Derived definitions.** Only literal declarations are located, so
  "definition_unlocated" can cover a definition that did not change. The
  audit therefore reports it as unverifiable, not as affected.

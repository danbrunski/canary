# B08 — behavior models informed by decoded ECU logic

Recorded 2026-10-04 from the [ECM application-code findings](../../ecu-firmware-analysis.md)
and the operator discussion about using them to improve behavior models and
the second brain. Follow [B00](B00-behavior-models-README.md) for validation
and publication, and [E00](E00-second-brain-README.md) for the Pi runtime and
evidence contracts. These are candidates and implementation tasks, not
measured model improvements or permission to refit/deploy unqualified models.

## Findings and implications

| Finding established by the code analysis | Proposed behavior-model use | Qualification still needed |
|---|---|---|
| `mem:ECM_FUEL_COMP_X_INPUT` (`0xFFFF118A`) is a throttle-derived, saturating fill estimate (alpha-N). BFS/Target AFR's X follow the MAF path. | Extend `air_charge_vs_rpm_throttle_did` with the actual fill coordinate and compare throttle-derived fill with MAF-derived charge under matched conditions. | The relative scale is established (0–224); absolute physical units remain open. Purge `w139E`, sensor-B fallback `w2CD0` and idle-air correction structure `w63FC` are now traced; full-chain live verification and branch coverage remain. This is a differently derived estimate, not an independent measured airflow sensor. |
| Fuel Comp uses rpm axis `0xCACD` and load axis `0xCB29`; UpRev labels the breakpoint lists the other way round. The grid is row-major, not transposed. | Compare rpm × fill-coordinate trim/delivery models with the existing rpm × OBD-load surfaces. Include active slot, table value and edge-clamp state as explanatory context. | Improved prediction or fault sensitivity must be demonstrated on the same eligible observations and independent drives. |
| Timing-trim axes are rpm `0xCC93` and BFS-X `0xCC83`, cells masked `& 0x7F`. The table is selected by the HI DET latch, `0xFFFF283F` bit 6: set → trim B (High Det), clear → trim A (Main) (FUN_0004e6d8, 0x04E6FE; corrected 2026-10-05 from an earlier `0xFFFF2842` claim, whose bit 2 only gates the knock controller's trim-difference branch). The latch is logged as `mem:ECM_HIGH_DET_FLAGS`. | Evaluate timing conditioned on the actual coordinate and selection state (283F bit 6); later use the decoded command as a reference. | Main/High Det role names are inferred from the latch's knock behaviour; final timing corrections and latch transitions are not yet qualified. A trim lookup is not final ignition timing. |
| Target φ lookup, pedal last-column override and active-map initialization are decoded. | Keep predicted-command versus observed-command error alongside B06's commanded-versus-measured mixture error. | Final-command applicability and interpolation accuracy remain E02 gates; transient exclusions and missing inputs cannot become normal results. |
| CAN and K-line DIDs can map to different RAM; CAN `0x1102` was mislabelled LTFT but is speed. | Audit artifact input lineage and supersede affected findings/models only where the wrong source was consumed. | Catalog correction is already recorded in D03.36; historical data/artifact impact still needs a source-specific audit. Current `TOTAL_TRIM_B1/B2` definitions consume Mode 01 STFT/LTFT, so contamination cannot be assumed for them. |
| `ZS44A-20261004` restored Fuel Comp's load axis and slot-1 cells; K, Load Point Scaler, Target AFR and timing-trim bytes were unchanged. | Use the recorded intervention to test whether decoded calibration changes predict condition-specific trim/delivery changes. | Outcome has not been established by these notes. Unchanged table bytes do not guarantee unchanged operating coordinates or final outputs. |

Evidence sources:

- [Fuel Comp writer and remaining unknowns](../../ecu-firmware-analysis.md#fuel-comp-load-input-0xffff118a-and-how-bfs-is-formed).
- [Corrected table axes](../../ecu-firmware-analysis.md#map-slot-tables-confirmed-from-the-code-that-reads-the-ram-copy)
  and [editor grid verification](../../ecu-firmware-analysis.md#uprevs-fuel-comp-display-checked-against-memory).
- [Existing timing/airflow limitations](B04-timing-and-airflow.md): timing
  remains exploratory; MAF-derived air mass and VE share measurement blind
  spots. The new coordinate is a candidate to evaluate against those gaps.
- [Pre-flash Fuel Comp prediction](../../../data/analyses/2026-10-04-fuel-comp-restore-prediction.md)
  and [revision ledger](../../../data/nissan-xterra-2008/tune_revisions.yaml).

## How this strengthens the second brain

Keep three distinct questions and evidence records:

1. **ECU computation:** does a decoded path reproduce the observed
   intermediate quantity or command? E02/E05 own that prediction.
2. **Engine response:** does the physical measurement agree with the
   observed command under validated timing and conditions? B06 and other
   command/response models own that residual.
3. **Historical relationship:** has either relationship changed from its
   condition-matched, validated reference? Behavior models and E03 own this.

For example, an agreeing φ prediction with lean measured mixture supports
investigating delivery, instrumentation or physical-model assumptions. A
disagreeing φ prediction first calls for checking model coverage, ECU state,
ROM identity and timing. Neither identifies a component failure by itself.
Throttle-fill/MAF disagreement may add discriminating evidence, but does not
yet establish that MAF bias and fuel-pressure sag are distinguishable.

Any learned offset or distribution around a decoded prediction remains a
separate, versioned behavior model. Preserve the original twin residual and
frozen reference; adaptation must not rewrite firmware constants, explain
away a developing fault or silently turn unknown logic into verified logic.
Shared-input channels and aliases are recorded as dependent evidence, never
extra independent votes.

## Checklist

- [~] **B08.1 — Audit observation and artifact lineage.**
  Trace recipe inputs, derived dependencies, historical decoders and stored
  artifacts for the corrected CAN DIDs and shared-register aliases. Check
  payload/source identity, not just channel names; a current catalog cannot
  make old decoded values correct. Re-decode recoverable raw samples, retain
  original provenance and supersede affected findings/artifacts through B03.
  **Accept:** a report lists affected, unaffected and unverifiable artifacts
  with source evidence. Mode 01 total-trim inputs are checked explicitly;
  the road-speed negative control cannot qualify as trim. PID 0x44 and
  target RAM agreement is counted as decode verification, not independent
  algorithm validation. No blanket retrain or silent feature replacement.
  *Audit done 2026-10-05 (revision 2, after verification); superseding the
  local LSTM copies and the unverifiable manifests awaits the operator.*
  [Report](../../../data/analyses/2026-10-05-b08-1-artifact-lineage.md) and
  [JSON](../../../data/analyses/2026-10-05-b08-1-artifact-lineage.json) come from
  [`tools/audit_artifact_lineage.py`](../../../tools/audit_artifact_lineage.py)
  over `src/analysis/artifact_lineage.py`.
  - **How each input is resolved.** Every input resolves to its source
    identity, in both the catalog committed before the artifact's build and
    the current one, following `tools/channel_renames`. The identity is a
    Mode 01 PID (via `obd_registry`), an OBD service, a DID number, a RAM
    address, or a CAN id and byte slice.
    - Derived dependencies are expanded from the **build-time** source,
      parsed rather than imported.
    - A derived definition that cannot be located there counts as
      unverifiable.
  - **As-recorded values.** RAM values are stored decoded at every build
    date, and broadcast values were stored decoded before P02.8. Both must
    have one committed decode across the recording window.
  - **Trim qualification.** A trim input qualifies only through a register
    whose term *and* bank match its declared claim
    (`trim_claims` in
    [`observation_aliases.yaml`](../../../data/nissan-xterra-2008/observation_aliases.yaml)).
  - **83 artifacts: 7 affected, 65 unaffected, 11 unverifiable.**
    - None consumed DID `0x1102` or `0x1123`–`0x1126` under any name.
    - All 65 behavior models are unaffected: the served set, `_prior`,
      `_candidates` and the review fixture.
  - **Affected:**
    - The laptop's 2026-09-20 LSTM and its `~/canary-data/models-0920a`
      copy. `did:ECM_INJECTOR_PULSE_1/2` were trained as raw counts, and
      `1156df0` pinned 0.01 ms after the build. This is a decode change, not
      a DID identity error, and it is not the deployed model.
    - Five research manifests in `_` directories. They consume the retired
      `decoded:ECM_CALC_LOAD_B` and `ECM_AIRFLOW_PROXY_23D`.
  - **Unverifiable:**
    - The 11 manifests are the deployed IF (09-20) and LSTM (09-24 na-r2),
      the six na/r2/r3 candidates, `_ablation-a03`, and the laptop's 09-20
      IF with its copy.
    - Each consumes derived channels with no literal build-time
      declaration:
      - `AFR_B1` and `AFR_CENSORED_B1`, in all 11;
      - `FUEL_RATE_LPH` and `ESTIMATED_POWER_HP/TORQUE_NM`, in 10;
      - `INSTANT_ECONOMY_*`, in 7.
    - Every identity they could trace is unchanged.
  - **Mode 01 total trim:**
    - `TOTAL_TRIM_B1/B2` resolve to PIDs `0x06`+`0x07` and `0x08`+`0x09`,
      from the build-time declaration.
    - Each leaf qualifies for its declared claim, same term and bank.
    - Stored payloads were re-decoded once per artifact file, over distinct
      sessions. For the served set that is 75 sessions and 347,612 rows;
      270,190 carry a payload, and all 270,190 match the stored value. The
      other 77,422 rows are python-OBD era and have no payload.
  - **RAM:**
    - The deployed LSTM and the r2/r3 LSTMs consume
      `mem:ECM_ENGINE_SPEED_RAM` and `mem:ECM_IGNITION_TIMING_RAM`.
    - One decode was committed across each recording window.
    - The stored rows (only 2 per channel in their sessions) reproduce under
      the recording catalog and agree with the current one.
  - **Payload identity beats session membership:** `air_charge` v2 fits on
    both rom-trace captures, which hold 231 raw replies each of `0x1102` and
    `0x1125`. Its own DIDs re-decode identically under the build-time and
    current entries.
  - **Negative controls:**
    - DID `0x1102` against wheel speed through `compare_channels`: n 202,
      slope 0.9950, r² 0.9994, `stable`. It is refused as LTFT B1, because
      register `0xFFFF11A8` holds road_speed.
    - DID `0x1125` claimed as LTFT B2 (its old label) is refused, because it
      reads the bank-1 word.
    - The old 2-byte entry decodes none of the 231 one-byte CAN replies.
  - **PID 0x44 against target RAM** is recorded as `decode_verification`
    only. The loader validates the enum.
  - **Stale labels:** 8 rom-trace sidecar labels are reported.
  - **Superseded findings:** two analyses carry a supersession note.
    Nothing was deleted.
  - No policy entry, retrain, feature change or catalog change was made.
  - **Tests:** `tests/test_artifact_lineage.py` has 26 tests, including:
    - `::test_a_name_that_moved_to_other_bytes_is_affected`
    - `::test_road_speed_cannot_qualify_as_trim_under_any_name`
    - `::test_a_bank_swap_cannot_qualify_either`
    - `::test_a_ram_decode_that_changed_inside_the_recording_window_is_not_unaffected`
    - `::test_stored_ram_windows_are_re_decoded_then_and_now`
    - `::test_derived_inputs_come_from_the_build_time_definition`
    - `::test_the_definition_parser_reads_every_historical_form`
    - `::test_mode01_channels_resolve_through_the_registry`
    - `::test_agreement_kinds_and_trim_claims_are_validated`
    - `::test_every_trim_channel_in_the_catalogs_declares_its_claim`
    - `::test_committed_history_reproduces_the_october_correction`
  - **Remaining (operator):**
    - Decide how to supersede the two 2026-09-20 LSTM copies. They are
      local model state, outside git.
    - [x] ~~Decide whether the unverifiable deployed manifests need their derived
      definitions declared literally, so a future audit can trace them.~~
      **Option (a) approved by the operator 2026-10-08, implemented 2026-10-08.**
      - **Declared literally.** Every `DERIVED_CHANNEL_INPUTS` and
        `DERIVED_CHANNEL_OPTIONAL_INPUTS` entry is now a literal: in
        `src/signals/derived.py` (AFR error, trip/performance channels,
        the seven committees and the two committee member transforms), in
        `DerivedSignal(...)`, or in the vehicle module's `SIGNAL_INPUTS` and
        new `DERIVED_CHANNEL_INPUTS` / `DERIVED_CHANNEL_OPTIONAL_INPUTS`
        tables (`vehicle_signals/nissan_xterra_2008.py`: AFR_B1/B2,
        AFR_CENSORED_B1/B2, both λ bank balances, BASE_FUEL_SCHEDULE,
        TARGET_AFR; vehicle DIDs stay out of shared code, R05.4).
        `derived.py` refuses to import (`DerivedDeclarationError`) when a
        vehicle table or the committee entries differ from what the code
        builds. `AFR_ERROR_B*` gained the `AFR_CENSORED_B*` group it reads.
      - **Parser.** `parse_derived_sources` reads the derived module plus
        `vehicle_signals/*.py` at the build revision (merged like
        `vehicle_signals._merge`), and optional inputs too. The audit now
        expands optional inputs as leaves and counts a changed optional set as
        `definition_changed`.
      - **Drift test** (`tests/test_derived_inputs_declared.py`, 43 tests).
        - **What it runs.** The pipeline's derivation chain, on frames built
          from each declaration: minimal group choices, optional inputs added
          one at a time, and each required input removed.
        - **What fails it.** A declared column that never changes the value. A
          value that moves when the undeclared columns are added. Those
          columns are every column a catalog or collector can produce: the
          Mode 01 registry, `dids.yaml` DIDs and RAM channels, the bindings,
          GPS, IMU, baro and the analog sensor. Each sweeps its catalogued
          plausible or encoding range where one is stated, so rpm covers
          0–7,000.
        - **Parser check.** It must locate every registry entry on the
          working tree, with the imported value.
        - **Can-fail proofs:**
          - `::test_an_undeclared_input_is_caught`: the S2 lambda dropped from
            the fuel rate's optional inputs.
          - `::test_a_declared_input_the_computation_ignores_is_caught`.
          - `::test_a_missing_alternative_is_caught`: the A/F voltage fallback
            dropped from AFR_B1.
          - `::test_an_undeclared_read_of_any_catalogued_column_is_caught`:
            the fuel rate made to read `obd:FUEL_LEVEL`. The verifier's
            `obd:FUEL_TANK_LEVEL` is in no catalog, so no frame can carry it.
          - `::test_an_undeclared_read_at_high_rpm_only_is_caught`: a read
            gated on rpm > 5,000.
        - **Tier.** Six of these measured ≥ 1.5 s serially (1.6–4.7 s on a
          loaded box) and are listed in `tests/slow_tests.txt`: the three meta
          tests above that plant a read, and the economy, fuel-rate and
          power/torque families.
        - **Blind spots. It does not prove every read is caught.**
          - A read gated on a *joint* condition the independently swept
            columns never meet together.
          - Shaped channels (A/F voltage, base fuel schedule, narrowband O2,
            PID 0x44, baro, GPS gates) do not sweep their full range.
          - A read of a column no catalog names.
          - Leaf comparison: a derived group whose leaves are already
            declared, such as AFR error's censor flag, looks the same as no
            group.
          - Producibility: a channel passes if *any* frame produces it. That
            is why D03.41 (AFR's voltage route needs baro) passes.
      - **Behaviour changes** (none alters an existing channel's value or any
        shipped profile's satisfaction):
        - **Two new registry entries,** `derived:OBD_SPEED_ACCEL_G` and
          `derived:GPS_SPEED_ACCEL_G`, absent at HEAD. Both are committee
          member transforms. `committee_channel_reason` still refuses them as
          features, and LON_ACCEL's declared leaves now include the speed
          sources they are differentiated from.
        - **Optional inputs are now expanded,** by both the lineage audit and
          `residual_chain.dependency_resolver`. Dependency sets grow:
          `derived:AFR_B1` resolves to the lambda PID, the A/F voltage DID
          *and* `obd:BAROMETRIC_PRESSURE`, and `derived:TARGET_AFR` adds
          `obd:COMMANDED_EQUIV_RATIO` (PID 0x44). A changed optional set at
          build is now `definition_changed`. `tests/test_residual_chain.py`
          passes unchanged.
        - **`AFR_ERROR_B*` gained the `AFR_CENSORED_B*` group,** so
          `resolved_dependencies(frame, "derived:AFR_ERROR_B*")` returns 3
          entries instead of 2. Its only consumer is
          `scripts/evaluate_behavior_models.py` `_input_resolution`, which
          writes the list into the report's `derived_input_resolution`. A model
          whose output is `AFR_ERROR_B*` would get a three-entry list per
          session, the middle one naming the censor-flag column or `None`.
          No served, prior or candidate behavior model targets `AFR_ERROR_B*`
          today, and nothing parses the list, so no report changes now.
      - **Audit re-run 2026-10-08** (same arguments, written outside the
        repo): **84 artifacts, 7 affected / 65 unaffected / 12 unverifiable**
        (was 83: 7 / 65 / 11). No common artifact changed verdict. The
        artifact set itself changed: the 09-20 LSTM copies now sit at
        `_superseded` paths (still affected), and a local copy of the deployed
        09-24 LSTM at `models/nissan-xterra-2008/lstm_autoencoder` is the
        twelfth unverifiable artifact.
      - **The deployed builds stay unverifiable at their build revision.**
        The literal declarations did not exist when they were built, so
        `AFR_B1`, `AFR_CENSORED_B1`, `FUEL_RATE_LPH` and
        `ESTIMATED_POWER_HP/TORQUE_NM` (and `INSTANT_ECONOMY_*` for the 09-24
        builds) remain `definition_unlocated` there. That is correct.
      - **Future builds are traceable.** All 11 earlier-unverifiable manifests
        were audited counterfactually, with the working-tree parse as the
        build-time definition. Every input resolved, and every one came out
        `unaffected`. This shows the audit can trace a build made from this
        tree. It does not verify the deployed models, which were computed with
        their own build-time code.
    - B03 has no manifest-level mark, and producibility cannot see a decode
      change on a column that is still producible.

- [x] **B08.2 — Add firmware-derived operating context.**
  Share a profile-backed provider with E02/E05/W08 for exact program/image,
  active slot, Fuel Comp rpm/fill coordinates, lookup value, clamp state,
  timing coordinate/select flag, pedal-enrichment flag and fuel-cut state.
  Use observed RAM coordinates before claiming to reconstruct their writers.
  Retain purge, learned idle-air correction and throttle fallback state when
  captured; distinguish the 224-count fill ceiling and the reciprocal-rpm
  saturation near 5,185 rpm. These are relative model coordinates, not VE.
  Include acquisition skew, freshness, raw/engineering units and applicability
  in both fit and serve contracts. Do not assume all historical/default
  captures contain these channels.
  **Accept:** known vectors reproduce the corrected axes and row-major
  layout; wrong-axis/transpose controls fail. Missing/stale/unknown state
  refuses by name, with no five-second forward-fill presented as a fresh
  observation. Both calibration images resolve through W08; existing
  recipes retain their identity until a new candidate is qualified.
  *Done 2026-10-05 (desk; freshness and skew limits are `proposed`).* The
  provider is [`src/analysis/operating_context.py`](../../../src/analysis/operating_context.py),
  declared in [`operating_context.yaml`](../../../data/nissan-xterra-2008/operating_context.yaml).
  - **Shared, not parallel.**
    - W08: tables, axes, fill curves and decodes are read by `rom_map.yaml` id
      through `table_values`.
    - E02: the slot comes from the twin's `map_identity` (`resolve_slot`, no
      restated channels), and lookups use `ecm_lookup.lookup_2d`.
    - E05: `check_image` decides program identity. Timing uses
      `selected_timing_trim`. Each lookup's applicability is computed from its
      application-model node.
  - **Observed coordinates only.** Every input names its RAM and relation
    (`identity`, `cycle_copy` or `state`). There is no `reconstructed`.
    - A dependency is cut when the context observes the RAM it writes. Fuel
      Comp's `fuel_comp_load_byte` is cut this way.
    - Fuel Comp stays `conditional` on `air_charge_ram_copy`, and the trim on
      `timing_trim_ram_copy`. The trim output is marked `intermediate`, never
      final timing.
  - **Known vectors and controls**
    (`tests/test_operating_context.py`, synthetic image):
    - `::test_fuel_comp_reads_rpm_rows_on_0xcacd_and_fill_columns_on_0xcb29`
      covers 4 hand-worked vectors (on-breakpoint, Q16 interpolated, both
      clamps, top breakpoint). Each one also equals E05's `rpm_load_lookup`.
    - `::test_timing_trim_reads_rpm_0xcc93_bfs_x_0xcc83_masked_and_selected_by_hi_det`
      covers 5 vectors: HI DET bit 6 set selects B and clear selects A; the
      flag is kept on a bit-7 cell; the pedal flag forces column 15; a cold
      engine reads A.
    - Controls:
      - `::test_transposed_and_axis_swapped_reads_fail_the_known_vectors`;
      - `::test_timing_trim_transpose_and_unmasked_controls_fail` (unmasked
        reads 128 counts high);
      - `::test_a_declared_axis_swap_is_refused_by_name` (`axis_mismatch`);
      - `::test_a_w08_map_with_the_axes_the_editor_shows_is_refused`;
      - `::test_an_application_model_reading_other_axes_is_refused`
        (`application_model_mismatch`).
  - **Refusals by name.**
    - Per item: `not_captured`, `missing`, `stale`, `clock_unacquired`,
      `wrong_units` (each `:<input>`), `skew:<lookup>`,
      `slot_unresolved:<reason>`, `coolant_blend_band`,
      `not_catalogued:<state>`.
    - At resolution: `image_hash_mismatch`, `wrong_program` (also a changed
      program literal), `table_unverified` (slots 2–5),
      `curves_not_monotone`.
    - `::test_a_five_second_old_window_read_is_stale_not_fresh`: a read held
      5 s is `stale:<input>`, never fresh.
    - `::test_a_resampled_grid_is_refused`: a wide frame with `__arrived`
      columns is refused `not_acquisition_timed`.
    - Fuel cut is decided by the newest read of the command or its sentinel,
      and refuses with that read's reason when it is unusable
      (`::test_a_refused_newer_command_read_is_not_covered_by_an_older_sentinel`).
    - A query earlier than a held read is refused `query_before_reads`
      (`::test_a_query_before_a_held_read_is_refused`). Fit and serve agree at
      that instant (`::test_fit_and_serve_agree_at_an_instant_before_later_reads`).
      `evaluate_frame` returns records in the caller's order
      (`::test_evaluate_frame_returns_records_in_the_callers_order`).
    - Known observation gaps are named applicability limits in the contract:
      `high_det_read_previous_cycle` (the trim lookup reads last cycle's
      latch) and `rpm_word_is_cycle_copy`
      (`::test_known_observation_gaps_are_named_applicability_limits`).
    - On the committed 2026-10-04 ram-study slice (174 command reads):
      - Fuel Comp is `stale:engine_speed_word` 153 times,
        `not_captured:fuel_comp_fill` 12 and `stale:map_magic` 9.
      - Timing never resolves.
      - Fuel cut reads 21 active / 153 not, matching the 21 sentinel reads.
      - At +4.9 s, all 174 are stale, each named for the newest read
        (`stale:command_word` 153, `stale:command_word_sentinel` 21).
  - **Both images through W08.**
    - `tests/test_operating_context_private_rom.py` (4 tests, real ROMs, skip
      when absent; ran here) stores both images in a `RomLibrary` and
      resolves them by sha256:
      - `a46baa…` (ZS44A-20260920) has load axis 16..144;
      - `5587f7…` (ZS44A-20261004) has 16..255.
      - Both have rpm axis 8..128, identical trims, different slot-1 cells.
    - Synthetic: `::test_both_calibration_layouts_resolve_through_the_w08_library`.
  - **Two saturations, reported separately.**
    - `fill_at_ceiling` is the fill curve at X = 0xFFFF.
    - `rpm_reciprocal_saturated` means `k × rpm word` has reached 0xFFFF.
    - On both real images: ceiling 224, k 158, saturation word 415
      (5,187.5 rpm).
    - `::test_fill_ceiling_and_rpm_saturation_are_reported_separately`.
    - `::test_the_fill_ceiling_and_the_reciprocal_rpm_saturation_come_from_the_image`
      cross-checks against the N02 `load_byte` / `rpm_reciprocal`.
  - **Fit and serve contracts.**
    - One `contract()` gives both. They differ only in `role` and share a
      fingerprint.
    - Per input: RAM, relation, `raw_units`, catalog stored decode,
      engineering unit, `max_age_s` and clock.
    - Per lookup: skew, layout and applicability.
    - The stored path replays reads through the live `ContextStream`
      (`::test_the_stored_path_replays_through_the_live_stream`;
      `::test_fit_and_serve_contracts_carry_skew_freshness_units_and_applicability`).
  - **History:** `coverage()` names inputs a capture lacks. The committed
    ram-study slice lacks the fill byte, HI DET, coolant DID and purge DID
    (`::test_the_october_4_ram_study_slice_names_what_it_lacks`).
  - **Recipes unchanged:** no recipe consumes the context, and
    `tests/test_recipe_catalog_golden.py` passes.
  - **Not done (outside this accept):**
    - Idle-air `0xFFFF63FC` and throttle fallback `0xFFFF484E`/`28B5` have
      no memory channel. They report `not_catalogued`; adding them needs
      `data/dids.yaml`.
    - No committed capture yields a fresh Fuel Comp tuple. B08.3 needs a
      capture that reads `spark`, `fuel_comp_x` and `flags_2841` back to
      back.

- [ ] **B08.3 — Evaluate throttle-fill and fuel-context candidates.**
  Extend existing B04 air-charge and B05 trim recipes rather than adding a
  parallel fitter. Compare the current rpm/throttle or rpm/OBD-load baseline
  with rpm/observed-fill and justified table-context alternatives. Keep
  pressure/IAT, fuel state, steady/transient regimes and calibration cohorts
  explicit. Fit no absolute-airflow or manifold-pressure interpretation to
  the raw fill byte without resolving its units. Saturation may reduce
  sensitivity at high load and must be reported.
  **Accept:** untouched-drive evaluation on the intersection of eligible
  samples reports physical-unit error, coverage, event rates and sensitivity
  to labelled MAF/throttle/common-input controls, plus exclusive coverage.
  Air mass/VE/common-MAF descendants do not acquire extra fusion votes.
  Improvements unsupported by excitation or pressure coverage stay
  exploratory. Name the targeted capture when RAM tuples are insufficient.

- [~] **B08.4 — Evaluate timing with actual lookup context.**
  Extend B04.2 with the verified rpm/BFS coordinate mapping and observed
  timing-selection state; preserve thermal and mode context supported by
  data. Compare against the existing timing surface on identical samples.
  Introduce a deterministic timing reference only after E05.4 qualifies its
  final-command chain, with any learned correction reported separately.
  **Accept:** B04/B02's declared timing-resolution and transient-control
  tests pass on independent drives before publication. A constant flag on
  one drive cannot validate both branches, and an isolated trim table cannot
  be labelled final timing or knock retard. Missing coverage remains named.
  *Desk part done 2026-10-08 (laptop, stored sessions). No stored drive can
  support the evaluation: the identical-sample comparison is empty, and the
  reason is named. Nothing was fitted or published.*
  - **Code.**
    - The B08.2 provider now emits `timing_coordinate`
      ([`operating_context.py`](../../../src/analysis/operating_context.py),
      `CONTEXT_EVALUATOR_VERSION` 3).
      - It holds the rpm word × BFS-X tuple under the trim lookup's own age
        and 0.25 s skew limits, plus the HI DET latch as read or its refusal.
      - It needs no image, slot or coolant, so it stays visible where
        `timing_trim` refuses.
    - [`timing_lookup.py`](../../../src/analysis/timing_lookup.py) fits
      nothing:
      - `identical_samples` takes one read of the existing surface's output.
        It pairs each of that surface's axes on the acquisition clock
        (`match_ordered`, each read used once, under a caller-stated policy).
        A policy looser than the lookup's 0.25 s skew is refused
        `pairing_exceeds_lookup_skew`, and the tool's `--pairing-ms` is held
        to the same limit. The coordinate's reads must lie within 0.25 s of
        the target read. Every unpaired read is counted under its reason; an
        axis read only on a logical clock is `clock_unacquired:<spec>`, not
        `not_captured`.
      - `score_identical_samples` grades those samples with B02's
        `evaluate_rows` on a frame stamped by `stamp_profile_exclusions`. A
        capture or tuning sample is therefore refused by name (R03.12), as it
        is at refit. It refuses a model whose output is not a declared
        final-timing channel (`not_final_timing:<spec>`), and samples read
        from another target (`target_mismatch`).
      - `trim_cell_coverage` places the coordinate on the image's two trim
        tables. The values are labelled `trim_*_intermediate`.
    - [`tools/timing_lookup_coverage.py`](../../../tools/timing_lookup_coverage.py)
      runs all of this per session.
    - A candidate surface would go through B04's recipe and fitter.
      It was not attempted:
      - all coordinate data are in non-eligible captures, which the fitter
        refuses;
      - there are no identical samples.
  - **Refusals (learned constraints).**
    - *Target.* `operating_context.yaml` declares the channels that observe
      final timing: `timing_trim.final_timing_channels` (required) is
      `obd:TIMING_ADVANCE`, `did:ECM_IGNITION_TIMING` and
      `mem:ECM_IGNITION_TIMING_RAM`. Any other target is refused
      `not_final_timing:<spec>`, so a trim or a coordinate can never be the
      graded quantity. This is separate from the existing no-knock-retard
      claim check in `test_behavior_resolution.py`.
    - *Branches.* `require_branch_validation` refuses in three cases:
      - `selection_unobserved`: no latch was read;
      - `branch_unobserved:<set|clear>`: a constant flag, which validates
        neither branch;
      - `branch_too_few_drives:<state>`: fewer independent drives than
        `min_drives`, the recipe's `min_validation_drives` and never below 2
        (`MIN_BRANCH_DRIVES`).
      - `branch_evidence` builds the drives itself with `group_drives`, so
        one drive split into two sessions (60 s apart) counts once.
  - **Run.** It covered all 14 sessions that hold any timing-coordinate
    channel, against the existing surface `timing_advance_vs_rpm_load` v3.
    Command:

    ```
    .venv/bin/python -m tools.timing_lookup_coverage \
      --session _capture-rom-trace-2026-09-21T142926 \
      --session _capture-rom-trace-2026-09-21T195647 \
      --session _capture-ram-study-2026-10-04T204557 \
      --session _capture-fuel-target-research-2026-09-21T013326 \
      --session _capture-fuel-target-research-2026-09-21T195844 \
      --session _capture-tuning-2026-09-21T223734 \
      --session _capture-tuning-2026-09-21T232153 \
      --session _capture-tuning-2026-09-22T170459 \
      --session _capture-tuning-2026-09-22T235224 \
      --session _capture-tuning-2026-09-23T022831 \
      --session _capture-tuning-2026-09-23T142926 \
      --session _capture-tuning-2026-09-23T164007 \
      --session 2026-09-21T201836 \
      --session 2026-09-22T171544 \
      --image "/mnt/c/Users/thebo/Desktop/Uprev/Tuned ROMs/ZS44A-Tuned-20260920.OsirisROM" \
      --image "/mnt/c/Users/thebo/Desktop/Uprev/Tuned ROMs/ZS44A-Tuned-20261004.OsirisROM" \
      --pairing-ms 250 --out <scratch>/timing_lookup.json
    ```

    Sessions are read from `~/canary-data/sessions/nissan-xterra-2008` (the
    default `--sessions-root`). The JSON is not committed.
    - `_capture-fuel-target-research-2026-09-21T013326` ran ZS44A-k106, whose
      image was not supplied. Its trim placement is refused
      `image_not_supplied:e01cc0cb45d5beb879f82931be3054c79b7be3955220b3d93045deaabd23f18e`.
      It has no BFS-X anyway, so no coordinate would place.
    - **Selection state.** HI DET (`0xFFFF283F`) was never captured, in any
      session. The branch verdict is `selection_unobserved`, with 1,756
      samples `not_captured:high_det_flags`.
      - The only indirect witness is `0xFFFF2842` bit 2, a lagged copy of
        the latch, on the 10-04 drive. It read 0x04 on all 191 reads: a
        constant flag on one drive, and an inference, not an observation.
      - Neither branch is validated.
    - **Coordinate observed** (rpm word × BFS-X, ≤ 0.25 s): only on the two
      `rom-trace` captures of 09-21, 1,534 / 1,547 and 222 / 224 reads.
      - `_capture-ram-study-2026-10-04T204557`: 0 / 180. The two memory
        windows are polled seconds apart: 142 `stale:bfs_x`, 37
        `skew:timing_coordinate`, 1 `missing:bfs_x`. The committed slice gives
        the same counts.
      - The other 11 sessions (fuel-target-research, tuning, two default
        drives) have the rpm word but no BFS-X: `not_captured:bfs_x`.
    - **Identical samples with the existing surface: 0, in every session.**
      - `rom-trace` polls no Mode 01, so there is no `obd:TIMING_ADVANCE`,
        rpm or load.
      - On the 10-04 drive, 1,131 timing reads: 605
        `stale:engine_speed_word`, 470 `unpaired:obd:RPM`, 54
        `unpaired:obd:ENGINE_LOAD` and 2 `stale:bfs_x`.
      - Had any paired, the B02 evaluator would still refuse them: all
        three coordinate captures declare `corpus_eligible: false` (R03.12).
      - No residual exists, so none is compared.
    - **Where the branch could show at all.** ZS44A-20260920 slot 1, slot
      assumed on `rom-trace`, which reads no map identity.
      - Trim A and trim B differ in 19 cells: rows 5–9 × columns 10–15, which
        is 2,000–3,600 rpm at BFS-X raw ≥ 88. With interpolation the lookup
        touches them between 1,600 and 4,000 rpm at raw > 80.
      - Of the 1,534 coordinates on 09-21T142926, 128 read a cell where the
        tables differ. There A − B is +1 count on 109, +2 on 13 and +3 on 6.
        Those are trim counts, which have no degrees, and are not final
        timing.
      - 09-21T195647 never reaches that region: 0 of 222.
      - Neither drive clamped on either axis. The 09-21 drives span 85 and
        15 lower-left cells.
    - **Thermal, mode and air-density context.**
      - Barometric pressure is only 79–83 kPa wherever it was read; `rom-trace`
        reads no baro. **No drive is below 76 kPa.**
      - Coolant ran 86–94 °C on the 10-04 drive; `rom-trace` reads none.
        Coolant DID `0x1101`, which the cold rule reads, was captured in no
        coordinate session. The pedal flags were read only on 10-04.
      - The resolution and transient-control tests (B04.3) were not run:
        there are no graded samples.
  - **Tests:** `tests/test_timing_lookup.py` has 27 tests, including:
    - `::test_the_coordinate_is_observed_without_identity_coolant_or_latch`
    - `::test_a_stale_or_skewed_coordinate_is_refused_by_name`
    - `::test_a_definition_without_final_timing_channels_is_refused`
    - `::test_a_trim_or_coordinate_cannot_be_the_timing_target`
    - `::test_each_target_read_is_paired_once_and_every_unpaired_read_is_named`
    - `::test_the_existing_surface_grades_identical_samples_and_refuses_a_tuning_capture`
      (residual = read − cell, per sample)
    - `::test_a_branch_is_validated_only_where_it_was_observed_on_independent_drives`
      (4 cases, including 500 set reads on one drive giving
      `branch_unobserved:clear`)
    - `::test_identical_tables_leave_no_branch_distinguishable`
    - `::test_the_october_4_slice_resolves_no_coordinate_and_observes_no_latch`
    - `::test_an_axis_pairing_looser_than_the_lookup_skew_is_refused` (a 5 s
      policy over reads 4 s apart) and
      `::test_the_tool_refuses_a_pairing_looser_than_the_lookup_skew`
    - `::test_an_axis_read_on_a_logical_clock_is_named_clock_unacquired`
    - `::test_scoring_refuses_a_model_that_does_not_grade_final_timing`
      (`derived:TIMING_TRIM`, and `target_mismatch`)
    - `::test_one_drive_split_into_two_sessions_validates_no_branch` and
      `::test_min_drives_cannot_fall_below_two`
  - **Remaining (closes the item):**
    1. Drives that are corpus-eligible, under R03.12 the default profile,
       and that read all of these in one tuple within 0.25 s:
       - the `spark` window (`0xFFFF11D8` with final timing `0xFFFF11C7`);
       - the `fuel_axis` window (BFS-X `0xFFFF103E`);
       - the `flags_2841` window (HI DET `0xFFFF283F`, pedal `0xFFFF2841`);
       - the map identity and coolant DID `0x1101`;
       - Mode 01 rpm, load and timing.

       Each latch state must be observed on ≥ 2 independent drives, and part
       of the driving must reach BFS-X raw > 80 between 1,600 and 4,000 rpm,
       the only region where the two tables read differently. Whether the
       clear state can be produced on demand is unknown. A drive below 76 kPa is also needed.
    2. Then a branch-conditioned candidate goes through B04's recipe and
       fitter. B04.3's resolution and transient controls and B02's gates
       must pass on untouched drives. That candidate also needs a serving
       contract that can condition on the latch, which B08.7 owns.
    3. A deterministic timing reference waits on E05.4.
  - **Operator decision 2026-10-08: recommendation (a), implementation
    stopped at the budget check.** The decision: add the `spark`,
    `fuel_axis`, `fuel_comp_x`, `flags_2841` (HI DET + pedal) and
    `uprev_state` (map identity) windows and coolant DID `0x1101` to the
    Xterra `default` (`data/nissan-xterra-2008/capture_profiles.yaml`, where
    `default` is data and may carry `memory:`). **`default` is unchanged.**
    Two findings stop it, and the profile data cannot fix either:
    - **Budget.** Measured with `preview_polling_plan`, the dashboard's and
      `/control`'s path, under `canary.pi.example.yaml`:
      - best-case requests 3.91 → 4.01 req/s, against the 8 req/s budget that
        `obd_poll_rate_hz: 1` derives;
      - memory adds 5 reads at 1/60 Hz, coolant adds 1 DID;
      - decoded rows 19.84 → 19.94 rows/s, about +0.004 % of the ~2,355
        rows/s a default drive stores. Storage and the digest are unaffected.

      The rate is fixed by the profile's 60 s DID windows. A memory read can
      only ride a window, so it gets one tuple a minute.
    - **Tuple skew fails at the serving budget.** The rpm word, BFS-X and HI
      DET are three service-0x23 reads, more than 63 bytes apart
      (`::test_the_coordinate_tuple_needs_three_separate_memory_reads`).
      `default` paces every request at 8 req/s, including inside a window.
      Mode 01 is silent from session entry to exit. The fake-clock loop
      measured nine windows in 600 s, with the Pi's collection settings and
      acquisition times paired per window:

      | budget | coordinate (rpm, BFS-X, HI DET) | with Mode 01 rpm/load/timing |
      |---|---|---|
      | 8 req/s (serving) | 0.500 s | 0.750–1.312 s |
      | 16 req/s | 0.250 s | 0.375–0.812 s |
      | 25 req/s | 0.160 s | 0.260–0.500 s |
      | 40 req/s | 0.100 s | 0.185 s |

      At the serving budget no tuple meets 0.25 s. Dropping `uprev_state`
      only reaches 0.375 s / 0.625–0.812 s.
      `default`'s budget is not profile data: `bus_budget_override` is armed
      only by the capture route. Pinned by
      `tests/test_default_profile_b08_context.py::test_the_b08_additions_miss_the_lookup_skew_at_the_serving_budget`
      (slow). That test is now
      `::test_the_b08_additions_meet_the_lookup_skew_at_the_serving_budget`:
      option (b) below closed the gap.
    - **The deployed LSTM learned the spark window.** The Pi's
      `lstm_autoencoder_2026-09-24T035125`
      (`tests/fixtures/deployed_manifests`) carries
      `obd:mem:ECM_ENGINE_SPEED_RAM` and `obd:mem:ECM_IGNITION_TIMING_RAM`
      (+`__mean`). They were scaled on one idle count: 775 rpm, std 12.5, and
      16 °, std 1. Those features are NaN at serve today. Polled by `default`,
      a 2,500 rpm read would reach that model at about 140 σ, once a minute.
      - `::test_default_polls_no_memory_channel_a_deployed_detector_learned`
        now refuses it. Its control is
        `::test_the_deployed_lstm_carries_the_spark_window_so_the_guard_can_fail`.
      - The leak's cause was a gate gap. `obd:mem:*` passes the source gate
        (its source is `obd`), and no serving gate covers it, because it is
        neither a PID nor a DID.
      - `FeaturePolicy.exclusion_reason` now refuses every memory channel as
        `memory_channel` (`feature_pipeline.MEMORY_CHANNEL_EXCLUSION`), and so
        does a derived channel that needs one. Behavior recipes still read
        them.
      - Tests: `tests/test_feature_policy.py::test_a_memory_channel_is_never_a_detector_feature_however_it_is_served`
        and `::test_a_derived_channel_whose_only_input_is_a_memory_channel_is_not_served`.
    - **Checked for when it does ship:**
      - `default` stays corpus-eligible: `_storage_policy` refuses otherwise,
        and R04.6 keys on the name.
      - A saved Pi override of `default` would shadow the change entirely.
        `layer_catalog` keeps the override and logs one WARNING per process.
        The API lists it only as `overridden`, with no "repo changed" flag.
      - `did:ECM_COOLANT_TEMP` would become a serving DID, and so a detector
        candidate at the next retrain: a feature-set change.
      - `ECM_HIGH_DET_FLAGS` is `hypothesized`.
        `test_memory_catalog_knock_lambda.py::test_new_channels_are_code_verified_and_not_yet_in_any_profile`
        refuses it in any profile until the D03.38 key-on read passes.
    - **Owed:** an operator choice among
      - raising the serving request pace (a code or config change);
      - narrowing B08.4's tuple to the memory-only target
        `mem:ECM_IGNITION_TIMING_RAM`;
      - a capture.

      Also owed: an LSTM retrain without memory features, and the fixture
      refresh, before `default` reads the spark window.
      *Chosen 2026-10-08: (b) a scheduler change and (c) the memory-only
      target as an interim. See the next sub-item.*
  - **Operator decisions 2026-10-08 (second round), recorded:**
    - **(b)** a scheduler change, so a session window's memory (0x23) reads
      go back to back, last in the window. Approved and implemented (below).
    - **(c)** as an interim, B08.4's tuple narrows to the memory-only target
      `mem:ECM_IGNITION_TIMING_RAM`, which arrives in the same read as the rpm
      word. While (c) is in use, the identical-sample comparison against the
      Mode 01 surface is given up. Implemented (below).
    - The LSTM is retrained without memory features before `default` reads
      any RAM. That is separate work.
    - The HI DET key-on read (D03.38) comes before HI DET joins any profile.
    - Coolant DID `0x1101` is accepted as a detector candidate once `default`
      polls it.
    - **`default` is still unchanged.** Adding the windows waits for the LSTM
      retrain and the key-on read.
  - **(b) Done 2026-10-08 (desk, fake clock). A window's memory reads go
    last, back to back, flush against the session exit.**
    - **Design.** [`can_obd_loop.py`](../../../src/collection/can_obd_loop.py)
      `_CanObdLoop._memory_burst`:
      - While any other session-window member is owed, memory reads are left
        out of selection.
      - Then the window waits until the reads, back to back, will finish
        where pacing's last read would have ended.
        - A paced read costs the longer of a budget slot and its turnaround.
          At 90 req/s a multi-frame read outlasts the 11 ms slot.
        - The turnaround is the previous burst's mean, or the last
          session-control turnaround before any burst has run.
      - Then every read is charged at once from the plan
        (`_BurstPlan.release`), the reads go without a slot between them, and
        the exit goes when pacing sent it. So do the lapse, Mode 01 and the
        next window.
      - A slot spent by anything else during the wait or the burst (a
        keep-alive, an identification read) moves the start and the charge
        out by exactly one slot (`_BurstPlan.others`).
      - Keep-alives (`keepalive_policy`) follow pacing:
        - one due by pacing's last read slot is sent. If the burst is about
          to start, it goes just before the burst, so it does not split the
          tuple;
        - one due later is not sent, as pacing did not send it;
        - once the burst runs past its paced end (an unanswered read's 1 s
          timeout), every due keep-alive goes, and it bypasses the pre-charged
          clock (`_session_budget_at`).
      - The next window is scheduled from when pacing would have closed this
        one (`DidWindowController.close_as_paced`). Without that, the
        end-to-start interval restarted 0.18 s later for every 60 s window.
      - A burst read's cadence grid steps from the window's opening
        (`_next_request_due_at(served_at=...)`). Measured from the late issue
        instant, a read issued 10 ms past its next grid point skipped a whole
        period. On a 4 s plan the spark window was then read only every other
        window. This also changes one measured rate; see "`tuning` at 8 req/s"
        below.
      - Untouched cases:
        - continuous windows (`did_continuous`), which never close, so there
          is no exit to sit against;
        - a window whose deadline falls before the paced slots run out. Its
          reads go at once and are charged as sent.
    - **What the ECM needs between 0x23 reads: nothing beyond the turnaround.**
      This is measured on stored sessions (laptop, read-only), not assumed:
      - `_capture-rom-trace-2026-09-21T142926`, 184 req/s override, continuous:
        - 7,821 memory reads, 5,211 of them issued straight after another
          memory read;
        - acquisition-to-acquisition gap: minimum 1.82 ms, median 6.15 ms;
        - memory request-to-response (`response_interval_s`): median
          2.11 ms, p95 5.11 ms.
      - `_capture-rom-trace-2026-09-21T195647`:
        - 870 memory-after-memory pairs, minimum 1.79 ms;
        - request-to-response median 2.05 ms, p95 4.95 ms;
        - 0 storage drops (`storage.dropped_records` delta 0);
        - `can0.rx_over_errors` +11 in 89.8 s. That was the MCP2515 rig,
          scope `controller`, reported but never a failure.
      - Earlier figures (`data/analyses/2026-09-20-uprev-usb-road.md`):
        - back-to-back 63-byte memory reads ran about 176 req/s;
        - 2,500/2,500 and 3,998/4,000 reads were answered at that pace;
        - single DIDs answered at 649 req/s (~1.5 ms each).
      - A burst is 3–5 reads once per 60 s window, about 15 ms on the truck.
        D03's MCP2515 overflow lead (bursty rounds, 36/min at ~650 req/s
        sustained) concerned the old rig. The MCP2518FD on HAT rev 2.0 has no
        overflow measurement yet: see Remaining.
    - **Budget evidence** (fake clock, `default` plus the B08 additions,
      Pi collection settings, 8 req/s, 600 s):
      - every window still has 16 requests: entry, 9 DIDs, 5 memory reads,
        exit;
      - entry to the next Mode 01 request is 1.885 s, and entry to exit
        1.760–1.762 s. HEAD gives 1.885 s and 1.760 s;
      - whole run: Mode 01 2,331 requests, DIDs 81, memory 45, session 19.
        These are identical to HEAD;
      - window entries match HEAD within 1 ms.
      - `tests/test_memory_window_burst.py::test_memory_reads_go_last_back_to_back_and_the_exit_follows`
        asserts that a window's N requests still span at least N−1 slots
        before the next Mode 01 request.
    - **Profiles with no memory reads are unchanged.** After the
      verification fixes, the Xterra `default` (300 s) and the generic
      `default` (300 s) were re-run with the verifier's harness. Their sent
      frames and readings are identical to a `git archive HEAD` export. The
      first run, before the fixes: the fake-clock harness
      ran the D03.38 verifier's method: the tracked catalog, the Pi's
      collection settings, a session-gated `ScriptedEcu`, 130 s per profile.
      It diffed every sent frame (time, id, payload) and every reading
      (channel, acquisition time) against a `git archive HEAD` export. All
      32 profiles without memory reads are **identical**:
      - the 6 tracked profiles without memory reads: the Xterra `default`,
        and the generic `default`, `fuel-map`, `tuning-did`,
        `broadcast-every-frame` and `fuel-pulsation`;
      - 2 saved `fuel-map` snapshots;
      - 24 DID-only twins: every memory profile with its memory reads removed
        (23 saved snapshots plus Xterra `tuning`);
      - 5 saved `tuning-did` snapshots from 2026-08 do not load at HEAD
        either (old DID names), so they were not compared.

      Profiles with memory reads (verifier's harness, after the fixes):
      - The continuous `rom-trace` snapshots are identical (first run).
      - `ram-study`, `fuel-target-research` 09-21T195844 and `tuning`
        09-23T164007 snapshots, 300 s each:
        - every channel's reading count is equal to HEAD;
        - window counts are equal (43, 160 and 120);
        - there are no lapses.
      - **Xterra `tuning` at its 90 req/s, 900 s:** every channel is within
        ±1 of HEAD. `COMMANDED_EQUIV_RATIO` is 2,752 → 2,751 and
        `FUEL_STATUS` 2,755 → 2,754.
        - Verification had found both 5.5 % low (2,752 → 2,614), and the
          core Mode 01 channels high.
        - The first report called this a batch-phase artifact. That was wrong:
          it was systematic. The cause was the exit landing where the burst
          ended instead of where pacing sent it. Pacing at 90 req/s runs on
          read turnaround, not on slots.
        - Mode 01 therefore resumed at a different point of the 0.5 s cycle,
          and a core-only round displaced the second batch, which carries
          0x44 and 0x03.
        - Now the burst ends at pacing's last read and the exit goes there.
          `::test_tuning_at_90_req_s_keeps_every_channel_at_its_paced_rate`
          (slow) pins every channel within ±1 of the burst switched off over
          300 s. The ±1 is the run's last partial window. The test fails with
          the old exit placement.
      - **Xterra `tuning` at 8 req/s, 900 s** (infeasible for that profile:
        its 0.5 s cycle needs 2.8 s at 8 req/s):
        - windows go 294 → 280, every window keeps pacing's span (20
          requests, 2.5 s), and there are no lapses;
        - the 0.125-weighted knock-sensor reads go 210 → 280 (read in every
          window), and the other memory and DID channels lose 14 each.
        - Their configured 0.5 Hz exceeds the window rate, so they are due
          in every window. HEAD read them in only 210 of 294 windows: issued
          late in the window, they missed the next grid point. That is the
          defect `served_at` removes.
        - A pacing-position anchor was tried. It gave 169, further from the
          configured rate, so it was dropped.
        - The double charge the verifier found (spans 3.145 s, 294 → 210
          windows) is fixed; see Tests.
        - Mode 01 pays for it too (verifier, against a fresh HEAD export,
          900 s): RPM, MAF, ENGINE_LOAD, THROTTLE_POS, TIMING_ADVANCE and
          O2_WR_LAMBDA_S1 −69 each (about −8 %, RPM 869 → 800);
          COOLANT_TEMP 477 → 337 (−29 %); INTAKE_TEMP −55; FUEL_STATUS −28;
          COMMANDED_EQUIV_RATIO −16; LONG_FUEL_TRIM_1 −14;
          BAROMETRIC_PRESSURE +70; LONG_FUEL_TRIM_2 +28; SHORT_FUEL_TRIM_1/2
          +13 each. Mode 01 requests 1,683 → 1,586, session frames 758 → 841
          (every window now needs its keep-alive).
        - All of it follows from the knock-sensor reads filling every window:
          with uniform memory weights the same run is identical to HEAD
          (280 = 280 windows, 9,621 = 9,621 frames, every reading count
          equal), so the burst itself changes nothing at 8 req/s.
        - `tuning` does not run at 8 req/s in practice: all 13 stored
          `_capture-tuning-*` snapshots carry `bus_budget_override: 90.0`,
          which `profile_capture` applies unless a request names another.
    - **Skew, re-run** (fake clock, nine windows in 600 s, the B08.4
      evidence's setup, acquisition-time pairs per window):

      | budget | coordinate (rpm, BFS-X, HI DET): HEAD → now | with Mode 01 rpm/load/timing: HEAD → now |
      |---|---|---|
      | 8 req/s (serving) | 0.500 → 0.042 s | 0.750–0.937 → 0.175–0.177 s |
      | 16 req/s | 0.250 → 0.042 s | 0.375–0.812 → 0.113–0.115 s |
      | 25 req/s | 0.160 → 0.042 s | 0.260–0.500 → 0.112 s |
      | 40 req/s | 0.100 → 0.042 s | 0.185 → 0.112 s |

      - The 0.042 s is five reads at the fake ECU's 10 ms turnaround: the
        tuple's three reads are the first, second and fifth of the burst. On
        the truck's ~2 ms turnaround it would be about 10 ms.
      - With Mode 01, the spread is the burst plus the wait for Mode 01 after
        the exit. At 8 req/s that wait is one slot (0.125 s); from 16 req/s
        it is the exit turnaround and the 50 ms grace.
      - Under (c) the narrowed tuple is the same three reads, with final
        timing in the rpm word's own read (0 s apart).
      - The HEAD column reproduces the B08.4 evidence, except the 8 req/s
        Mode 01 upper bound (0.937 here, 1.312 there).
  - **(c) Done 2026-10-08 (desk). A memory-only target, declared in data and
    held to one read.**
    - [`operating_context.yaml`](../../../data/nissan-xterra-2008/operating_context.yaml)
      `timing_trim.memory_only_targets` declares
      `mem:ECM_IGNITION_TIMING_RAM` with `same_read_as: engine_speed_word` and
      its evidence: the `spark` window is one 19-byte read, and the byte is
      `equals_did 0x110A`.
    - Schema (`MemoryOnlyTargetDecl`) refusals:
      - the target is not one of `final_timing_channels`;
      - `same_read_as` is not a coordinate input;
      - (`check_catalog`) the two channels are not one catalog memory window.
        This is `catalog_mismatch`, "not one memory window".
    - The contract carries `memory_only_targets`. The definition file changed,
      so the context fingerprint changes.
    - [`timing_lookup.py`](../../../src/analysis/timing_lookup.py)
      (`TIMING_LOOKUP_VERSION` 3):
      - `memory_only_samples(provider, long, session, target)` pairs each
        target read with the coordinate from its own read.
        - It refuses `not_same_read:<input>` when the rpm word is another read.
        - The other inputs come from the target's **read group**: the reads
          between the previous and the next read of the target's window.
          - It takes the first read at or after the target, else the last one
            before it.
          - Refusals: `no_read_in_group:<input>`; `skew:target_coordinate`
            when that read is beyond 0.25 s; `other_read_group:<input>` when
            the provider still took an earlier group's read.
          - The first rule (evaluate just before the next rpm-word read) let
            the next burst's BFS-X, read before its own spark read, replace
            this burst's. Verification found this. The probe case is now in
            `::test_a_memory_only_sample_takes_its_coordinate_from_the_same_read`.
        - It keeps every provider refusal.
      - `require_memory_only_target` refuses `not_memory_only_target:<spec>`.
        That includes `obd:TIMING_ADVANCE` and `did:ECM_IGNITION_TIMING`.
      - **The Mode 01 comparison is refused by name.** `identical_samples` and
        `score_identical_samples` refuse `memory_only_target:<spec>` for a
        model whose output is the memory-only target and whose axes are not
        all memory.
      - Grading memory-only samples with the Mode 01 surface is still
        `target_mismatch`. Every earlier refusal is kept.
    - [`tools/timing_lookup_coverage.py`](../../../tools/timing_lookup_coverage.py)
      reports `memory_only` per session. Re-run on the three coordinate
      sessions:
      - `_capture-rom-trace-2026-09-21T142926`: 1,539 / 1,547 (7
        `no_read_in_group:bfs_x`, 1 `skew:target_coordinate`);
      - `_capture-rom-trace-2026-09-21T195647`: 224 / 224;
      - `_capture-ram-study-2026-10-04T204557`: 1 / 181 (180
        `skew:target_coordinate`). Its windows were seconds apart.
      - Nothing is graded. HI DET was never captured, so
        `selection_unobserved` stands.
  - **Tests (b, c):**
    - `tests/test_memory_window_burst.py` (9 fast, 1 slow). Each runs the real
      loop on the fake clock. They show:
      - memory goes last and back to back, with the exit after it;
      - the tuple and its Mode 01 leg fit 0.25 s at 8 req/s, and final timing
        has the rpm word's acquisition time;
      - a DID-only profile stays paced;
      - a continuous window stays paced;
      - a window whose deadline cuts the wait reads at once;
      - a keep-alive does not split the burst;
      - the next window does not move;
      - `::test_a_keepalive_in_the_wait_is_charged_once`: a keep-alive in
        the wait spends one slot and no more. Window counts equal the run
        without it, and each window spans N−1 to N slots;
      - `::test_unanswered_memory_reads_never_hold_the_keepalive_past_s3`:
        0x23 requests are lost before the ECM and do not refresh S3
        (S3 3.0 s, 30 s blackout). There are 0 lapses, the session-control
        gap stays ≤ the keep-alive interval plus one request timeout, and
        Mode 01 never gaps 10 s;
      - `::test_tuning_at_90_req_s_keeps_every_channel_at_its_paced_rate`
        (slow).
      - Controls, each checked by hand with the fix removed: the keep-alive
        split test, the window test, the double charge (release from the live
        clock), the keep-alive held for the whole burst (S3 test), and the
        old exit placement (the tuning-rate test).
      - The verifier's silent-read scenario: `default` plus the B08 windows,
        every 0x23 unseen, S3 3.0 s, 30 s blackout, 600 s. HEAD and now both
        have 0 lapses and 9 windows. The longest session-control gap is
        2.127 s at HEAD and 2.586 s now (one read timeout). The verifier's
        own configuration had measured 6.78 s and 2 lapses before the fix.
    - `tests/test_default_profile_b08_context.py::test_the_b08_additions_meet_the_lookup_skew_at_the_serving_budget`
      (slow): two windows each at 8 and 40 req/s; memory spread < 0.125 s,
      joined ≤ 0.25 s.
    - `tests/test_timing_lookup.py` (+9):
      - `::test_the_memory_only_target_is_final_timing_read_with_the_rpm_word`
      - `::test_a_memory_only_target_must_be_final_timing_on_a_coordinate_input`
        (2)
      - `::test_a_memory_only_target_in_another_read_than_its_input_is_refused`
      - `::test_only_a_declared_memory_only_target_has_a_memory_only_tuple` (3)
      - `::test_a_memory_only_sample_takes_its_coordinate_from_the_same_read`
        (a burst, a different read, no read in the group, missing, a
        continuous capture's next spark read, and the verifier's two-burst
        probe)
      - `::test_the_memory_only_target_is_never_graded_on_the_mode01_surface`
  - **Remaining after (b)/(c):**
    - `default` still reads no RAM. It waits for the LSTM retrain (separate
      work) and the HI DET key-on read.
    - The burst is checked on the fake clock only. On the truck, read the
      achieved tuple spread from a capture's acquisition times, and
      `can0.rx_over_errors` on the MCP2518FD.
    - Items 1–3 above still close the item. Under (c), item 1's Mode 01 leg
      is not needed, and the comparison against the Mode 01 surface is
      given up until (c) is lifted.

- [~] **B08.5 — Connect distinct residuals to investigation evidence.**
  Feed B08 context and qualified behavior results into E03 while keeping
  ECU-computation, command/response and historical residuals separately
  identifiable. Reuse B06's units, fuel-state and transport-delay contracts.
  Mint E01 evidence with input-dependency groups and exact ROM/model versions.
  **Accept:** known cases distinguish a wrong ROM/branch, an injected
  delivery deviation with correct command, and missing target data.
  Investigation cards name the earliest supported disagreement without
  declaring a unique cause. Learner updates cannot remove or renormalize the
  original twin discrepancy, and alias/common-input evidence is not doubled.
  *Plumbing done 2026-10-06, revised 2026-10-07 after verification (laptop,
  labelled synthetic known cases). No B08 residual is qualified yet, so
  every one is diagnostic-only.*
  - **Minter:** [`src/analysis/behavior_residual_evidence.py`](../../../src/analysis/behavior_residual_evidence.py),
    `mint_behavior_residual`.
    - It is the only producer of `behavior_relationship` evidence, and its
      class is fixed to `historical`.
    - Every record names the exact model: name, version and artifact sha256.
    - Every record names the exact ROM: image sha256, program, revision,
      slot, and the B08.2 context-contract fingerprint. `RomIdentity.from_context`
      takes these from `operating_context.ResolvedContext`.
    - `fuel_state` must be one of B06's `Regime` values, `fuel_cut` or
      `not_fuel_conditioned`. There is no default.
    - `residual_inputs` (the channels the residual is computed from) is
      required.
    - **Diagnostic-only is enforced.** Without a `qualification` the result is
      unevaluable `unvalidated` with no residual; the number survives only as
      the `diagnostic_residual` value.
    - **A qualification must be real.** It has to resolve to a current,
      unretired `behavior_model` `Reference` whose `source_ref` is exactly
      `<name>@<version>`, on the same program and calibration. Otherwise
      `QualificationError`; nothing is silently downgraded.
    - Without a resolved ROM the result is `unknown_context`.
  - **Chain:** [`src/analysis/residual_chain.py`](../../../src/analysis/residual_chain.py).
    - **Stages come from the producer, never from a label.** The ECU
      computation stage holds `target_phi_twin` only; command/response holds
      B06 `lambda_delivery_error` only; historical holds `change_detection`
      and `b08_behavior_residual`.
    - Refused with `NotAResidualError`: any other producer, a mislabelled
      evidence class, and a synthetic detector run (`data_origin: synthetic`).
    - **`fuel_pressure_droop` stands in no stage** (`EXCLUDED_PRODUCERS`, a
      named refusal). Droop is pressure against airflow inside the delivery
      path, not commanded against measured mixture. In a stage, a pressure
      fall would read as a stage disagreement. Its job is to separate the
      command/response hypotheses, which E03.1/E03.2 already do with it.
    - **B06's contracts are checked, not restated.** A command/response record
      needs `ResidualDefinition.unit` and a `Regime` region
      (`ContractMismatchError`). A sign is read the way E03.1 reads it
      (`delivery_investigation.observation_of`): B06's floor, stable across
      the delay bracket, out of sample (`INFORMED_REFUSAL`). An unsigned
      result is `diagnostic_only`, with a reason starting `sign_refused`.
    - **Change detection counts only while its frozen reference stands.** The
      record's `change_reference:<fingerprint>` reference must be on the
      ledger, current and unretired. Otherwise it is `diagnostic_only`
      (`reference_lapsed`). This is the same re-check a B08 qualification
      gets.
    - `drive_residuals(ledger, session)` applies `build_chain`'s own tests:
      a stage producer, that producer's evidence class (so E03.3
      `policy_frozen` records are excluded), not synthetic, and sessions
      split on commas the way `_sessions` splits them.
    - **Each stage gets one state:** `agrees`, `disagrees`, `diagnostic_only`,
      `unevaluable` (each record's reason kept) or `absent`.
    - **The outcome** is `disagreement`, `agreement` (every stage present
      agrees) or `not_established`.
    - `earliest_disagreement` is the first disagreeing stage in causal order.
      `upstream_unestablished` names any earlier stage that does not agree.
    - **One drive per chain** (`MixedDrivesError`).
    - **Dependency groups.** Inputs resolve through B08.1's `resolve_input`
      against the current catalog. Derived channels expand to their leaves.
      Leaves then group by the profile's `observation_aliases.yaml`: a shared
      register, a dependent relation, or else the identity itself.
      - Within a stage, a record that shares a group with a counted record is
        `dependent_on` it and is not counted.
      - Groups shared *across* stages are reported. The twin and B06 share
        PID `0x44`/RAM `0xFFFF26BE`.
    - **The ledger refuses rewrites.** `MemoryLedger.append`
      (`src/evidence/ledger.py`) raises `ForeignSupersessionError` when a tool
      tries to invalidate evidence another producer minted. That covers a
      re-mint of its subject, an explicit `supersedes` and a `Retraction`.
      The operator may still retract anything.
      - The chain keeps `DiscrepancyRewriteError` as a read-time check, for
        ledgers written before this rule.
      - An investigation update keeps every earlier observation. A
        same-producer recompute is followed to its current record.
      - An operator-retracted observation is dropped from the update. It is
        named in the unknowns and the narrative, and does not block the
        update.
    - **The investigation** (`investigate_residual_chain`, E01 card):
      - The title names the earliest supported disagreement.
      - The stage's alternatives are all open and none is credited.
      - For command/response, the alternatives are E03.2's own result:
        `observation_of` for every record in the stage, then
        `fault_model.evaluate_observations(...).consistent`, plus a
        transport-delay pairing alternative. Opposite signs in the stage raise
        `FaultModelError`.
      - Unknowns name the unestablished stages, the dependent records and the
        shared observations. Engine health is always `not assessed`.
  - **Known cases** (`tests/test_residual_chain.py`, 24 passed, none in the
    slow tier):
    - **Drives.** Labelled synthetic drives each hold a twin tuple
      (`tests/test_target_phi._session`, command at 10 s, reads to 20 s) and
      one cruise-then-WOT pull (5 s idle, 5 s cruise, 6 s WOT, 5 s idle, at
      4 Hz). That is the least each producer needs to reach the verdict each
      test asserts; the tests assert verdicts, not row counts.
    - **Built lazily.** Each drive spec is built and minted on first use and
      kept for the module, so an xdist worker builds only the drives its tests
      use. B06 runs once per session and every test copies its drive's
      ledger. The application model is parsed once, with libyaml
      (`::test_the_cached_application_model_is_the_loaders`). The dependency
      resolver reuses B08.1's libyaml loader.
    - **Timing.** The first drive test in a worker carries the worker's
      one-time costs.
      - Serial, quiet box: about 1.3 s.
      - `-n 4 -m "not slow"` on a loaded box (load average 5–8), three runs:
        1.8–3.0 s each for the four tests that came first in their workers.
      - Deselecting those four moved 2.9–3.3 s onto the next first tests
        (measured), so none was added to `tests/slow_tests.txt`.
    - **Minting.** Residuals are minted by the real `evaluate_and_mint` and
      `mint_delivery_error`.
    - **Twin table.** The twin uses E02.6's labelled blocker-lifted table.
    - **Cases:**
      - `::test_a_wrong_rom_is_an_ecu_computation_residual` reads another
        image's table: ECU `disagrees`/`leaner`, delivery `agrees`, earliest
        `ecu_computation`. The wrong image sha is shown.
      - `::test_a_wrong_branch_is_an_ecu_computation_residual` reads the
        pedal flag clear on an override word: earliest `ecu_computation`.
      - `::test_a_delivery_deviation_with_a_correct_command_is_a_command_response_residual`
        (4 % lean, twin `zero`): earliest `command_response`/`lean`. The card
        lists MAF error, sag and sensor bias, plus pairing. No-fault and the
        fixed-quantity error are absent. Both older loops still refuse the
        other's evidence.
      - `::test_missing_target_data_is_named_empty_never_a_pass`: both stages
        `unevaluable` `missing_input`, outcome `not_established`, no
        residual, no hypothesis.
      - `::test_shipped_blockers_leave_the_upstream_stage_unestablished`: with
        the real blockers the twin is `unvalidated`, so the lean delivery is
        earliest with `upstream_unestablished = (ecu_computation,)`.
      - `::test_an_unqualified_delivery_sign_is_diagnostic_only` (no
        condition-matched floor): the stage is `diagnostic_only`, the reason
        starts `sign_refused`, and the outcome is `not_established`, not
        agreement.
      - `::test_opposite_signs_in_the_command_response_stage_are_refused_by_the_fault_model`.
    - **Stored session tried:** `_capture-ram-study-2026-10-04T204557`, from
      `~/canary-data`. It is uncovered, so it is not a known case.
      - Twin with the shipped table: unevaluable `unvalidated`, with 0
        eligible tuples.
      - Twin with the blockers lifted: unevaluable `uncovered`, because there
        is no eligible steady tuple.
      - Delivery: unevaluable `uncovered` at enrichment. The gates removed:
        fuel_cut 33, measured_missing 209, measured_rail 1, post_fuel_cut 13,
        target_transient 72, source_not_valid_in_regime 1.
      - Chain: `not_established`.
  - **Learner and rewrite:**
    - The ledger rule's own fast test is
      `tests/test_evidence_memory.py::test_only_the_producer_supersedes_its_evidence_and_only_the_operator_retracts_it`.
      It covers a cross-producer subject re-mint, an explicit `supersedes` and
      a tool retraction, all refused, plus a same-producer recompute and an
      operator retraction, both allowed. With the check disabled, it fails.
    - `::test_a_learned_correction_cannot_absorb_the_twin_discrepancy`: a
      qualified learned offset beside the twin record reads `agrees`. The ECU
      stage still counts the original record with its own residual, and the
      update keeps every observation. The ledger refuses the learner's append:
      a re-mint of the twin's subject, and an explicit `supersedes` of the
      twin record (`ForeignSupersessionError`). The ledger size is unchanged.
    - `::test_only_the_operator_retracts_another_producers_evidence`:
      - A tool's retraction of the twin record is refused by the ledger.
      - The operator's retraction of the delivery record is accepted.
      - The next update drops the retracted record, names it in the unknowns
        and narrative, and the stage reads `absent`.
  - **Alias and common input** (`::test_alias_and_common_input_evidence_is_counted_once`):
    - Counted once: Mode 01 LTFT B1 with DID `0x1125`, and
      `derived:AIR_MASS_PER_CYCLE` with `obd:MAF`.
    - Controls, both counted: STFT vs LTFT, and DID `0x1102` (road speed) vs
      LTFT.
    - Also `::test_dependency_groups_resolve_through_the_profiles_aliases`.
  - **Diagnostic and qualification:**
    - `::test_an_unqualified_b08_residual_is_diagnostic_only` (exact ROM and
      model labels asserted).
    - `::test_a_qualified_b08_residual_counts_until_its_reference_is_retired`
      (a hardware intervention returns it to `diagnostic_only`, outcome
      `not_established`).
    - `::test_a_qualification_must_name_this_model_version_and_rom`,
      `::test_without_a_rom_a_b08_residual_is_unknown_context`,
      `::test_the_rom_identity_comes_from_the_operating_context`,
      `::test_records_outside_the_chain_or_from_another_drive_are_refused`
      (mixed drives, a foreign producer, a relabelled class, an off-regime
      region and a non-percent unit).
  - **Historical stage:**
    - `::test_drive_residuals_returns_only_what_the_chain_accepts`:
      - A comma-pooled change record is found.
      - `policy_frozen` and synthetic records are excluded.
      - `build_chain` refuses both of those.
    - `::test_a_change_alarm_counts_only_while_its_reference_stands`:
      - With no reference record the alarm is diagnostic.
      - With the reference recorded it gives earliest `historical`/`up`.
      - Once a hardware intervention retires the reference it is
        diagnostic again.
    - `::test_fuel_pressure_droop_stands_in_no_stage`.
  - **Remaining (closes the item):**
    1. A qualified B08 residual on a real drive. That needs B08.3/B08.4
       results from a fresh Fuel Comp tuple capture (`spark`, `fuel_comp_x`
       and `flags_2841` read back to back), and a `behavior_model`
       qualification `Reference` published through B03/B08.7.
    2. A real drive that carries the twin tuple and a WOT delivery residual
       together. No committed fixture does.
    3. The Pi run through E04: job wiring, and persistence across a restart.
    - Not wired: no job, route or MCP tool calls the chain yet.

- [ ] **B08.6 — Evaluate the October 4 Fuel Comp intervention.**
  Preserve the original pre-flash prediction as written, including its
  historical uncertainty; add new interpretation and results as linked
  records. Old image:
  `a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50`;
  restored image:
  `5587f7e1e6dbd0fcb9ffe0c857cc0cafebbe11bf306636c0f3265b903254d253`.
  The ledger's flash boundary is `2026-10-04T23:02:33.183902Z`; the RAM-study
  fixture precedes it. At matched input points, reproduce the proposed table
  multiplier changes, then test physical outcomes on independent later drives.
  Match rpm × raw fill and relevant thermal/barometric/fuel-state context;
  retain commanded φ, injector pulse, mixture and bank trims. Account for
  LTFT reset/relearning rather than equating early total trim with the prior
  baseline. Both axis and cells changed, so their individual physical effects
  are not isolated by this intervention.
  **Accept:** tool evidence reports the recorded direction/magnitude tests
  and condition-matched noise floor, supporting and refuting observations,
  coverage, exclusions and remaining confounders. No effect and insufficient
  evidence are distinct outcomes. Unchanged target/timing bytes are not
  asserted to guarantee unchanged commands. This evaluation issues no flash.

- [ ] **B08.7 — Publish qualified models and verify their Pi integration.**
  Reuse B03 compatibility/publication gates and E04's shared runtime. Record
  calibration applicability and affected references without conflating a
  calibration edit with a moved RAM address. Preserve old references and
  feature contracts used by existing detectors; extend manifests explicitly
  for any new feature selection. Required evaluation/promotion can complete
  on the Pi after collection stops; live prediction stays bounded.
  **Accept:** held-out evidence justifies each promoted candidate, live and
  stored results agree, and the combined Pi workload meets E04's measured
  budget without blocking collection. An unqualified candidate remains
  diagnostic-only with its limitation visible in second-brain cards.

## Order and ownership

B08.5 supplies E03.1/E03.2 with distinct residual evidence and observation
dependencies; its separate learned corrections support E03.5. B08.6 supplies
the intervention case for E03.6 without rewriting the original prediction.

Start with B08.1/B08.2 and the Fuel Comp/throttle-fill candidates (B08.3),
then timing (B08.4). B08.6 is the concrete intervention evaluation and can
start once its input/provenance audit passes. B08.5/B08.7 connect only
qualified outputs to the Pi brain. E05 owns reconstructed ECU calculations;
B08 owns their use in behavior-model context and validation. Existing B04,
B05 and B06 checkboxes retain their own acceptance and historical evidence.
No queue order or existing model status changes merely because this plan exists.

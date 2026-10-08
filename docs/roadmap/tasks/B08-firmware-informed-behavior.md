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
    - Decide whether the unverifiable deployed manifests need their derived
      definitions declared literally, so a future audit can trace them.
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

- [ ] **B08.4 — Evaluate timing with actual lookup context.**
  Extend B04.2 with the verified rpm/BFS coordinate mapping and observed
  timing-selection state; preserve thermal and mode context supported by
  data. Compare against the existing timing surface on identical samples.
  Introduce a deterministic timing reference only after E05.4 qualifies its
  final-command chain, with any learned correction reported separately.
  **Accept:** B04/B02's declared timing-resolution and transient-control
  tests pass on independent drives before publication. A constant flag on
  one drive cannot validate both branches, and an isolated trim table cannot
  be labelled final timing or knock retard. Missing coverage remains named.

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

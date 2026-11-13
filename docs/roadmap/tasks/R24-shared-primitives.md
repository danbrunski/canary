# R24 — Shared primitives: the same small thing written many times

Part of the 2026-11-12 review ([R22](R22-refactor-README.md)). Every area
reviewer found copies of the same handful of helpers. They are collected here
because each one is a single change across packages, and because several of
them are copies *with drift*, which is where the defects are. Counts are
`grep` snapshots from 2026-11-12; recount before starting. Nothing here has
been implemented. Shared acceptance rules are in R22.

R01 (September) did this once, and those results held: `artifact_hash`,
`timefmt`, `jsonl_store`, `coerce`, `signals/time_lookup` and
`signals/pairing` all exist and are used. Each item below is a helper that
either did not exist then or exists now and is bypassed.

**How to land these.** One item per commit. A mechanical migration (no
behavior change) and a unification that changes an output are different
commits; items that change an output say so. Where copies differ, the
migration keeps each caller's current output behind an explicit argument, and
a fixture per caller proves it.

- [x] **R24.1 A refusal that carries a reason.** Eighteen exception classes
  in `src/` hand-write `self.reason = reason` with their own `__init__`,
  some with `.message`, some with `__reduce__` and some without (an exception
  without it does not survive the trip back from a spawn child with its
  fields). Callers translate one to another by hand in about as many places.
  - [x] `ReasonedRefusal(Exception)` at layer 0: `reason` (a stable code),
    `detail`, pickle-safe. Existing classes subclass it and keep their names,
    codes and messages.
    Evidence: `src/refusal.py` (new; `"refusal": 0` in `tests/test_layering.py`
    `LAYERS`, added first). `ReasonedRefusal(reason, detail)`; `str(exc)` is
    `"<reason>: <detail>"` through an overridable `render`; `message` is a
    read-only alias of `detail`; `__reduce__` rebuilds through
    `rebuild_exception(cls, args)` = `cls.__new__(cls, *args)` plus the instance
    dict, so a subclass with a constructor of its own (the trap) round-trips with
    no `__reduce__` of its own (checked: with the base's `__reduce__` deleted,
    the toy subclass fails `TypeError ... missing 1 required positional
    argument`). All 18 hand-written classes migrated, names/codes/messages/
    `str(exc)` unchanged, each still catchable as the builtin it was
    (`ValueError`, `RuntimeError`; a refusal raised in a pydantic validator is
    turned into a validation error only if it is one): 11 `(reason, message)`
    classes with `.message`, 4 without (`TimingLookupRefusalError`,
    `AxisPairingRefusedError`, `FuelPlantRefusalError`, `SimDesignRefusalError`;
    their `__reduce__` sliced `str(self)`), `TripEndDigestUnavailableError`
    (`str` is the message alone), `LoadAxisUnavailableError` and
    `_RecipeSkipError` (one argument; `str` is the reason; `candidate` kept);
    `EditSpecRefusalError` subclasses `RomEffectRefusalError` and inherits.
    `rg 'self\.reason = reason' src`: 18 hand-written (HEAD 912c3238) -> 2
    (the base at `src/refusal.py:55`, and its docstring). Additive: every
    migrated class gains `.detail`; seven gain `.message`, now a read-only
    property; `vars(exc)` is `{reason, detail}`. No caller assigns `.message`.
    Not covered (outside `src/` or a different shape): `scripts/canary_desk.py`
    `DeskRefusalError`, `tools/rom_sim.py` `CliRefusalError`,
    `nonvolatile_write.AuthorizationRefusedError` `(code, detail)`. `class \w+(Error|Refusal)\(`: 175 -> 177 (the two bases).
    Regrowth guard: `tests/test_source_rules.py` row `hand-written-refusal-reason`
    (AST: `self.reason = ...` in a class with an `*Error`/`*Exception`/
    `*Refusal` base, outside `src/refusal.py`; allowlist empty; run against a
    `git archive HEAD` tree it finds 18, now 0).
  - [x] Extend `tests/test_exceptions_pickle.py` to enumerate every subclass
    by reflection, so a new one cannot skip the round-trip.
    Evidence: after the existing whole-`src` enumeration (`walk_packages`
    imports every module), `_subclasses()` walks `__subclasses__()` of both
    bases: 19 `ReasonedRefusal` and 4 `StatusError` subclasses, asserted to be
    exactly the named sets and a subset of `CLASSES`. Per subclass: pickle,
    `copy.copy` and `copy.deepcopy` keep type, `args`, `str`, `vars` and
    `reason`/`detail`/`message` (or `status`/`detail`); a subclass defining its
    own `__reduce__` fails (`test_a_refusal_subclass_has_no_pickle_rule_of_its_own`);
    `_RecipeSkipError` round-trips with `candidate` set; toy subclasses with a
    different `__init__` and a keyword-only field round-trip; `str(exc)` of the
    three non-standard classes is pinned. `pytest tests/test_exceptions_pickle.py`:
    226 passed.
  - R29.3's `StatusError` (the four `(status, detail)` classes and the app
    exception handler) is the HTTP face of this. Design them together.

- [ ] **R24.2 `StrictModel`.** `class _Strict(BaseModel)` with
  `extra="forbid"` is defined 51 times, 28 of them in `src/analysis/`.
  `analysis/dyno_contract.py` has the only named `StrictModel`. Not every copy
  has the same config (frozen or not, `populate_by_name`).
  - [ ] `StrictModel` and `FrozenStrictModel` at layer 0. Migrate the copies
    whose config is identical first; list the ones that differ and keep them
    until each difference is shown to be unused.
  - Must not change: any schema's accepted input. The YAML fixtures under
    `data/` loading unchanged is the test.

- [x] **R24.3 Load a YAML file into a model, once, with the file named in
  the error.** `Model.model_validate(load_yaml(path))` is written 35 times in
  `src/`, `scripts/` and `tools/`; thirteen sites also hash the file and cache
  the result, each its own way. Only `dyno_profile` guards an empty file.
  Costs measured on the laptop: `ecu_application_model.yaml` 130 ms per
  parse; `data/dids.yaml` (296 kB) about 10 ms to re-validate per call, and
  `read_channels` does that per call (R26.3); `/status` re-reads several per
  poll (R29.9).
  - [x] `load_yaml_model(path, Model)`: a validation error that names the
    file, the sha256 of the raw bytes for provenance, and a cache keyed by
    `(path, mtime_ns, size)`. Callers that mutate the result get a copy and
    say so.
    Evidence: `src/yaml_load.py` `load_yaml_model(path, Model) ->
    LoadedModel(model, sha256)`, `YamlModelError(ValueError)` (names the file,
    keeps the pydantic detail as message and `__cause__`), cache keyed by
    `(resolved path, st_mtime_ns, st_size, Model)` (64 entries),
    `clear_yaml_model_cache()`. Empty file or a non-mapping document raises
    `YamlModelError` naming the file (the `dyno_profile` rule, now the
    helper's); failures are not cached. `tests/test_yaml_load_model.py`
    (18 collected): file named, empty / comment-only / list, sha256 == hashlib
    over the raw bytes, cache hit is the same object, per-Model-type entries,
    re-read on mtime change and on size change, failure not cached.
    Racy-stat guard (verifier finding): on this ext4/WSL2 kernel 1987 of 2000
    back-to-back same-size writes kept the same `st_mtime_ns`, so a same-size
    rewrite after a load was served stale (and the cached sha256 described the
    wrong bytes). An entry cached within `RACY_WINDOW_NS` (2 s) of its file's
    mtime is verified by reading the bytes and comparing sha256 (re-validate on
    a difference); once verified after the window it settles and a hit is one
    `stat`. Tests: `test_a_same_size_rewrite_straight_after_a_load_is_not_served_stale`
    (200 same-size rewrites, no `os.utime`) and
    `test_a_same_size_rewrite_with_an_identical_stat_is_reread` both fail with
    the guard disabled (checked: `RACY_WINDOW_NS` forced negative, 2 failed,
    16 passed) and pass with it; `test_a_settled_file_is_trusted_by_its_stat_alone`
    and `test_a_racy_entry_settles_once_the_window_has_passed` pin the
    steady-state cost (no byte read).
    Mutation audit of `load_did_map` / `load_bindings` callers: none mutates
    (static scan: the `model_copy(update=...)` sites build new models; the
    `limits.py` `entry.unit` write is a builder entry, not the catalog). No
    caller needed `model_copy(deep=True)`, so none pays for one; the helper's
    docstring says callers must not mutate. Contract change: `load_bindings`
    and `load_did_map` now raise `YamlModelError` (a `ValueError`) rather than
    `ValidationError`; no `src`/`scripts`/`tools` caller catches
    `ValidationError` from them (`plausibility_live` catches `ValueError`);
    two tests (`test_bindings_decoder`, `test_bindings_validation`) updated.
    Not migrated: the other ~30 `model_validate(load_yaml(...))` sites and
    `dyno_profile` (own guard kept).
  - [x] Migrate R26.3's catalogs and R29.9's status blocks first; they are
    the ones with a measured cost. Scope done: the R26.3 catalogs
    (`load_did_map`, `load_bindings`, hence `_Catalogs`) and the status path's
    catalog read (`memory_verify_summary`, `src/control/calibration.py`, calls
    `load_did_map`, so box 1 covers it; `src/dashboard/status.py` is no longer
    held, R29.14 committed). R29.9's other `/status` costs (manifests, session
    list, CPU) are JSON and directory reads, not YAML-model work, and belong to
    R29.9. The other ~30 `model_validate(load_yaml(...))` sites (and
    `dyno_profile`, which keeps its own guard) migrate as their files are
    touched; none was migrated here.
  - Must not change: the bytes-keyed flash-history cache
    (`test_an_edited_flash_history_is_reread`).

- [~] **R24.4 Timezone coercion and ISO formatting.**
  `tzinfo is None` appears on 53 lines in 37 files. `_aware` is defined 8
  times, `_utc` twice more, `_iso` 13 times, and the copies do not agree:
  some `replace(tzinfo=UTC)` (leave a non-UTC aware value alone), some
  `astimezone(UTC)` (convert); `_iso` emits `+00:00` in some files and `Z` in
  others. `src/timefmt.py` has `format_timestamp` and `parse_iso_utc` and
  nothing for this.
  - [x] `timefmt.as_utc(dt)` (naive is UTC, aware is converted) and
    `timefmt.format_epoch(seconds)`. Migrate the identical copies and the
    eight sites in `features/live.py` first.
    Done, with two more helpers because the copies split three ways, not two.
    `src/timefmt.py` gains `as_utc` (convert), `assume_utc` (naive is UTC,
    an aware value is returned untouched, offset kept), `utc_datetime` (the
    pandas flavor: any stamp-like, floored to microseconds) and
    `format_epoch(seconds, *, zulu=False, nanoseconds=False)`.
    `as_utc` replaces `_utc` in `models/evaluation_corpus`,
    `preprocessing/drives`, `analysis/labelled_events`, `storage/sessions`
    (`_as_utc`), plus inline copies in `alerts/event_logger` (x2),
    `scripts/derive_sessions`, `tools/replay_window`. `assume_utc` replaces the
    keep-aware copies: `_aware` in `vehicle/interventions` and
    `vehicle/tune_revisions` (plus its two inline sites), `_utc` in
    `features/bus_freshness`, `features/trip_integrals`,
    `control/twin_identity._epoch`, `scripts/_cli`,
    `scripts/evaluate_detectors`, and every `features/live.py` site (the file
    has five, not eight: lines 788, 803-804, 967, 1210, 1264 at the time).
    The `live.py` sites stay keep-aware, not `as_utc` as the item wrote: a
    copy that leaves an aware non-UTC value alone would otherwise start
    emitting `+00:00` for it. `utc_datetime` replaces `_aware` in
    `analysis/delivery_evidence`, `compare_evidence`, `twin_evidence` and
    `change_evaluation` (its `None` guard stays local).
    Evidence: `tests/test_timefmt.py` (533 cases) holds each deleted copy
    verbatim as an oracle and compares helper to copy on naive, aware
    non-UTC, aware UTC, pandas Timestamp, with and without microseconds,
    ns-bearing and string forms (values and `isoformat()` text).
    `tests/test_twin_runner.py` used `target_phi._iso` and now calls
    `format_epoch(..., nanoseconds=True)`.
    Left alone, not tz coercion or a different contract:
    `analysis/replay_window._iso` (`astimezone(UTC)` treats a naive value as
    local time, the others as UTC), `candidate_promotion._aware` and
    `models/adaptive_candidate._aware` (`datetime.fromtimestamp(t, UTC)`, an
    epoch constructor), `analysis/kline_exchanges._iso`,
    `session_timeseries._iso`, `preprocessing/imu_session._iso`,
    `dashboard/readers._iso`, `evidence/cards._iso` (plain `.isoformat()`,
    no coercion, a naive stamp stays naive).
  - [ ] The remaining ~35 `tzinfo is None` lines. Most are plain-`datetime`
    copies of the `assume_utc`/`as_utc` pattern, not pandas:
    `storage/sessions.py` (~114), `analysis/plant_validation.py` (~720, 895,
    902, 933), `vehicle_state.py` (~429), `control/nonvolatile_write.py`
    (~160), `analysis/artifact_lineage.py` (~1028, 1033),
    `analysis/fuel_comp_restore.py` (~563), `analysis/digest.py` (~242),
    `daemon/proposals_live.py` (~280), `analysis/behavior_validation_schema.py`
    (~74), `scripts/tag.py` (~132, 195, 216), `tools/import_uprev_log.py`
    (~180), `control/updater.py` (~118), `control/brain_reads.py` (~85). The
    rest are pandas `tz_localize`/`tz_convert` on a Timestamp or index
    (`session_files`, `evaluation_corpus` ~81-82, `anomaly_injection`,
    `retraining_policy`). Each site needs its own check of which variant it
    is (convert vs keep-aware) against the oracles in `tests/test_timefmt.py`.
    This is desk work.
  - [x] For each `_iso` that writes to a stored file, keep that file's
    current suffix behind an argument. Unifying `Z` and `+00:00` in stored
    artifacts is a separate, deliberate change, and probably not worth
    making.
    Done: `format_epoch` takes `zulu` (`plausibility/evaluator`,
    `scripts/decode_uprev_flash`, `signals/committees` write `Z`) and
    `nanoseconds` (`cold_start_inventory`, `target_phi`, `target_phi_stream`
    go through `pd.Timestamp(seconds, unit="s")`, so a float such as
    1708740829.9991355 is stored as `...49.999135494+00:00`; the default
    rounds to `...49.999135+00:00`, which `plant_validation` stores).
    `test_format_epoch_flavors_reproduce_each_copy` runs 505 epochs through
    all four spellings against the deleted `_iso` bodies, text for text, and
    `test_nanoseconds_flag_is_load_bearing` pins the example. 13 `_iso`
    defs: 7 replaced here (`decode_uprev_flash`, `cold_start_inventory`,
    `plant_validation`, `target_phi`, `target_phi_stream`, `evaluator`,
    `committees`), 6 left (`replay_window`, `kline_exchanges`,
    `session_timeseries`, `imu_session`, `readers`, `cards`; see above).
    Verified 2026-11-13: an independent differential over adversarial
    inputs (fold, extreme dates, ZoneInfo, compact and offset strings,
    µs rounding boundaries, negative epochs, numpy scalars) gave 0 diffs
    from the HEAD bodies. No stored suffix changed.

- [x] **R24.5 Canonical-JSON hashing.** An AST scan found 38 sites that hash
  `json.dumps(…, sort_keys=True)` with sha256, in four flavors: compact
  separators with `default=str` (9, identical to
  `evidence/provenance.hash_json`), compact without (10), default separators
  without (15) and with (4). Stored manifests, cache keys and `meta.json`
  files pin which flavor each site must keep. `plausibility/limits.py`
  records what a hash that silently changes costs: 1 GB of orphaned streams.
  - [x] `sha256_json(payload, *, compact=True, default=None)` and
    `sha256_bytes` in `src/artifact_hash.py`, which is already "the one
    sha256 function". Migrate the nine `hash_json`-identical sites first: no
    stored hash changes.
    Evidence: both added; `ensure_ascii`/`allow_nan`/encoding stay at
    `json.dumps`/`.encode()` defaults (no flag for them). The nine
    compact+`default=str` sites now call `sha256_json(..., default=str)`:
    `evidence/provenance.hash_json` (now a one-line wrapper),
    `behavior_models.fingerprint` (keeps its `sha256:` prefix outside the
    call), `evidence/records._digest`, `feature_cache._stable_hash`,
    `adaptive_candidate._hash`, `candidate_store._key`,
    `change_detection.reference_fingerprint`, `evaluation_cache.digest`,
    `vehicle/interventions.source_digest`.
  - [x] The rest move with explicit flags and a one-line comment naming the
    stored artifact that fixes the flavor, with a fixture per caller.
    Evidence: 32 call sites in 25 files migrated in all. Compact, no default
    (9): `adaptive_candidate` and `change_detection` `.sha256()`,
    `twin_command_change`, `rom_effects` (`digest` and `edit_set_sha256`),
    `rom_edit_spec.sha256`, `brain_jobs_store.job_id_for` (`bj-` + 20 hex),
    `limits.fingerprint`, `sim_contract.Scenario.scenario_hash` (its public
    `canonical()` had no other caller and went). Default separators, no
    default (11, `compact=False`, each with a comment naming the stored
    artifact): `operating_context.contract`, `paired_scenarios` (x2),
    `plant_validation` rules hashes (x4), `fuel_plant` baseline,
    `capture_profile_model.profile_digest`, `decode.on_read_hash` and
    `imu_stream.key` (both `[:16]`). Default separators with `default=str`
    (3): `datasets/export.config_hash`, `derived_store._hash` (a `str` payload
    still hashes as raw text), `twin_runner.run_digest`.
    No hash moved. `tests/test_artifact_hash.py` (37 tests) keeps the deleted
    bodies verbatim as oracles in all four flavors and compares digest
    strings over 15 payloads (nested dicts, -0.0, NaN/inf, 1e-7, 5e-324,
    2**70, non-ASCII keys and values, tuples vs lists, int keys, datetime and
    Path and a custom `__str__` through `default=str`, key order; both sides
    must raise `TypeError` together without a default). The nine
    payload-level sites plus `job_id_for` and `derived_store._hash` are called
    for real against those oracles; the model-backed sites are called on real
    data with `sha256_json` spied and checked against the oracle on the
    payload they built, and against stored values: the `fuel_plant.yaml`
    baseline sha256 (`6aec1db5...`, also `plant_baseline_sha256` in the stored
    sim design under `~/canary-data/sim/predictions`),
    `plant_interventions.yaml` frozen rules sha256 (`f522ff53...`), and all 10
    `contract_fingerprint`s stored in `data/nissan-xterra-2008/behavior_models.yaml`.
    A flavor table over every `sha256_json(` call in `src/` (AST) fails if a
    site's flavor changes. Separate before/after run of the same inputs at
    HEAD source vs the tree: baseline, interventions rules, EditSpec fixture
    sha, `on_read_hash`, 5 capture profile digests and all 41 stored
    behavior `contract_fingerprint`s (4 `_candidates/` files + the live one)
    were byte-identical. `config_hash` was checked in-process against its
    oracle (it embeds tree-relative paths, so two trees cannot be compared).
    Pins held: `test_feature_pipeline_golden` (cache key), the behavior
    fingerprint tests, and the 101 test files that import a touched module
    (fast tier 2,338 passed, `slow` tier 244 passed, `ml_training` 125 passed;
    `tests/test_adaptive_candidate.py::test_the_learning_kernels_cannot_reach_the_twin`
    now allows the stdlib-only leaf `src.artifact_hash` beside `src.evidence.*`).
    Left alone, not migrated (a different canonical form, or not a whole-
    payload hash): `sim_design.SimDesign.sha256` (`allow_nan=False`, stored
    design ids, `canonical_json()` is public); `plant_validation._digest`
    (an HMAC over `default=list`); `isolation_forest._training_data_hash`,
    `sequence_dataset.training_data_hash`, `sim_montecarlo.digest` and
    `tools/bench_ecu_twin._digest` (each feeds a JSON header into a streaming
    digest with more parts, so there is no single payload to hash);
    `sim_cells` (a dedupe key, not a hash). Nothing sat in `rom_editor*` or
    `tools/rom_sim.py`. No `ensure_ascii`/`allow_nan` flag was added: the
    one site that needs `allow_nan=False` keeps its own encoder.
  - Must not change: any stored hash. `test_feature_pipeline_golden` (cache
    key) and the behavior-fingerprint tests are the pins.

- [~] **R24.6 One bin rule.** `behavior/surface_eval.bin_indices` is the
  rule (R05.10 fixed a right-closed `pd.cut` that dropped left-edge values).
  It is bypassed by: `tuning.py`'s per-band grouping with `pd.cut` and
  default right-closed edges, the R05.10 shape again;
  `tuning_digest._bands`; `workbench._cells` (`right=False`);
  `signals/afr.afr_error_surface`; `np.digitize` in `dyno_inertia` and
  `dyno_power`; `fuel_comp_fit._maf_bins`. A value exactly on an edge can land
  in different cells depending on which page drew it.
  - [x] `cell_codes(values, edges, *, out_of_range="drop" | "clip")` beside
    `bin_indices`, and an edge-value test (values equal to every edge) run
    against each caller before and after.
    Evidence: the rule moved to `src/signals/bins.py` (new, layer 0:
    `signals/afr` is layer 0 and could not import `behavior`, layer 4):
    `bin_indices` / `bin_index` byte-for-byte, plus `cell_codes` (`-1` = no
    cell; `out_of_range` has no default; NaN/inf get no cell in either mode)
    and `cell_names` (`[lo, hi)`, last `[lo, hi]`). `surface_eval` imports it;
    its five importers point at `signals.bins`. All seven copies migrated with
    `out_of_range="drop"`, and the `outside_rpm_bands` delivery gate now is
    `cell_codes(...) >= 0` so it keeps exactly what the bands bin.
    Before → after at edges (500, 1000, 2000, 4000), probed at HEAD and in the
    tree: delivery bands and `tuning_digest._bands` (right-closed `pd.cut`)
    500 no band → first, 1000/2000 band below → band starting there;
    `afr_error_surface` (`include_lowest`) 1000/2000 cell below → cell starting
    there, first cell's `rpm_low`/`load_low` 499.999 → 500 exactly;
    `workbench._cells` (`right=False`), `dyno_inertia._binned`,
    `dyno_power` (`np.digitize`) and `fuel_comp_fit._maf_bins`
    (`searchsorted`) 4000 no cell → last cell. Labels change from
    `(lo, hi]` to `[lo, hi)` / last `[lo, hi]` (`delivery_evidence._band_key`
    strips either bracket, so evidence keys are unchanged).
    Real data, raw `obd:RPM`/`obd:MAF` samples whose cell moves (drives
    2026-10-10T153117, 2026-10-27T173734; captures
    `_capture-tuning-2026-11-11T174602`, `-11-12T034249`): delivery/WOT bands
    23 / 20 / 33 / 20 (of 2444 / 1529 / 3193 / 2544; 2500 rpm entering the
    first band is most); AFR surface rpm axis 31 / 20 / 26 / 31; workbench,
    fuel-comp MAF 0 (no sample on 7000 rpm or 250 g/s). `compute_tuning_digest`
    on both captures, HEAD vs tree: WOT seconds per band, delivery `n`, gates,
    per-band `error_pct`, `band_averaged_pct` and `pooled_pct` identical, only
    the labels changed (no gated sample sits on an edge). The committed
    delivery slices (`tests/fixtures/delivery_investigation`) do move:
    band-averaged error, HEAD → tree, `…09-23T164007` +3.304 → +3.239 %
    (two 4500 rpm samples and one 5000 rpm sample move up a band),
    `…09-22T235224` +0.736 → +0.714 % (n 21 → 22: its 2500 rpm sample is no
    longer gated out), `…hybrid-research-09-17T191712` −0.462 → −0.443 %,
    `…T201000` −0.657 → −0.607 %; `…09-23T142926` and `…09-22T170459`
    unchanged. 9 of 223 gated samples sat on an edge. All stay inside the
    2.50 % floor, every `sign` is still `0`; `test_delivery_investigation.py`
    pins updated to those values.
    Deployed behavior models unchanged: all 12 surfaces of
    `data/nissan-xterra-2008/behavior_models.yaml` evaluated on 2026-10-10T153117
    (1851 rows) and 2026-10-27T173734 (1242 rows) at HEAD and in the tree,
    max |Δ expected| = max |Δ deviation| = 0, refusal reasons and 2D cell
    assignment (`containing_cell_indices`) identical on every row
    (`cat_warmup` spoke on neither drive).
    Tests: `tests/test_bin_rule.py` (new: the rule at every edge, out of
    range, NaN/inf, agreement with `bin_indices`, `cell_names`; every caller
    at every edge; an AST guard refusing `pd.cut` / `pd.qcut` /
    `np.digitize`, under any module alias or a `from` import, in `src/`
    outside `signals/bins.py`, allowlisting `fuel_plant_eval.py`'s two
    `right=False` MAF-band cuts, the open sub-item below), and
    `test_afr.py::test_afr_error_surface_bins_every_breakpoint_by_the_one_rule`.
    `delivery_evidence`'s delivery-error and droop mints hash
    `src/signals/bins.py` in their `code_version` (both gate on the rpm
    bands); `compare_evidence` does not bin. E03's quoted delivery figures
    updated with a dated note;
    label expectations updated in `test_tuning_digest.py` (2) and
    `test_wot_profile_facts.py` (1).
  - [ ] `src/analysis/fuel_plant_eval.py`'s two MAF-band
    `pd.cut(..., right=False)` calls (the `maf_band` labels over `MAF_BANDS`),
    found after the list above was written: a sample exactly on the top MAF
    edge falls in no band. Move them to `cell_codes(..., out_of_range="drop")`
    with `cell_names` labels, so the top edge joins the last band (and the
    last label reads `[lo, hi]`), then delete the file from `UNMIGRATED` in
    `tests/test_bin_rule.py`. An output change like the rest of R24.6: count
    the samples that move on real drives. Coordinate with R24.7, which
    reshapes this module.
  - **This one changes outputs** wherever a copy disagrees with the rule. Its
    own commit, with the cells that move listed in the message.

- [ ] **R24.7 Time-lookup leftovers and per-sample series helpers.** R11
  made `signals/time_lookup` the one previous / nearest / bracket lookup.
  Still outside it: `fuel_comp_inputs._bracket`; two `_value_at_or_before`;
  `BaroSamples.as_of`; `analog_pulsation._rpm_by_seq` (a `merge_asof`).
  `fuel_comp_restore._bracket` is at-or-*after* on purpose: document it, do
  not migrate it. A second family has grown beside it:
  `fuel_plant_eval._series` / `_pair` / `_bracketed` / `_nearest_value` /
  `_window` against `delivery_evidence._bracketed_state` / `_window_means`,
  and `plant_validation.ecu_output_reads` repeats the first sixty lines of
  `fuel_plant_eval.build_samples`.
  - [ ] Migrate the leftovers to `time_lookup` with the semantics each
    already has.
  - [ ] `signals/series.py` for the per-sample helpers (windowed mean,
    bracketed state, steady-condition masks), used by `fuel_plant_eval`,
    `delivery_evidence` and `plant_validation`. M01.14 changes
    `_window_means`' consumer; do the move first.

- [x] **R24.8 Source-scan guards for rules that are already decided.**
  `tests/test_yaml_load.py` refuses a direct `yaml.safe_load` anywhere; the
  same pattern costs a few lines each for:
  - the literal `"telemetry-*.parquet"` outside
    `storage/session_files.PARQUET_PATTERN` (31 occurrences in `src/`,
    `scripts/`, `tools/` today; allowlist and shrink);
  - `.iterrows()` and `apply(axis=1)` in `src/` (R26 found none on session
    frames; keep it that way);
  - sorting session directories by name (`AGENTS.md`: "re-derived four
    separate times");
  - `setInterval` outside `hooks/` in the dashboard (R28.3);
  - `from tests.test_` (R30.2); `systemctl restart canary` outside
    `restart_canary.sh` (R30.1).
  - [x] One `tests/test_source_rules.py` with a table of (pattern, where it
    is allowed, the incident it names), so adding a rule is adding a row.
    Done 2026-11-12: seven rows, each an allowlist ratchet (per file: hits
    allowed today and why; more fails, fewer fails "lower the count", zero
    fails "remove it from the allowlist"). Rows (allowlist files/hits at
    HEAD): `parquet-glob-literal` 20/30 (the 31st is `session_files.py`
    itself, exempt), `per-row-dataframe-python` 2/2 (`.iterrows()` only; no
    `apply(axis=1)` in `src/`), `sessions-ordered-by-directory-name` 10/13,
    `dashboard-setinterval` 11/11 (`hooks/`, so `useTagQueue`, and test files
    are exempt by path), `tests-import-other-tests` 58/118 (R30.2 box 1),
    `tests-redefine-example-yaml` 60/60 and `tests-recompute-repo-root`
    177/183 (R30.8 box 2). Each row carries a `bad` and a `good` snippet;
    `test_the_predicate_flags_the_bad_snippet_and_not_the_good_one` and
    `test_a_violation_in_a_scanned_file_is_reported` (a scratch tree) prove
    each row bites; `test_the_ratchet_reports_growth_shrinkage_and_stale_entries`
    proves the three failure modes. Allowlists were built from a
    `git archive HEAD` copy and every row is clean against it. Row 3 only
    sees `sorted/max/min` over `.iterdir()` (or `.glob("20…")`) ordered by
    name; it does not see `.sort()`, `os.listdir` or `sessions[-1]` after an
    unsorted listing, and some of its allowlisted hits are "iteration only,
    downstream not reviewed" (named per file). The restart guard
    (`tests/test_daemon_restart_guard.py`, shell scripts, own comment
    handling and tests) stays its own file: moving it would not be a pure
    move.

- [x] **R24.9 Locked JSONL append.** `_lock_for(path)` with a module-level
  registry is defined three times (`control/brain_jobs.py`,
  `evidence/ledger.py`, `vehicle/vehicle_ledger.py`, one with a non-reentrant
  lock). `brain_jobs` and `evidence/ledger` each also carry an `fcntl.flock`
  append with torn-tail repair and an incremental tail index;
  `models/candidate_store` has a fourth `flock`. `jsonl_store.append_jsonl_line`
  says "callers that need locking take it themselves".
  - [x] `path_lock(path)`, `append_jsonl_line_locked(path, payload)` and a
    small `TailIndex` in `src/jsonl_store.py`; move `BrainJobStore` and the
    evidence ledger onto them.
    Done: also `file_lock` (path lock + flock, for the evidence ledger, which
    holds the flock across refresh, decide and write) and
    `append_jsonl_line_repairing` (the same append with no locking, for use
    inside `file_lock`: a second `flock` on another handle in one process
    blocks forever). The three `_lock_for` registries are gone
    (`test_no_module_keeps_its_own_lock_registry`). `path_lock` is an `RLock`:
    brain_jobs and the ledger nest (`submit` -> `get`/`transition`; the ledger's
    reads inside `append`), and the vehicle ledger's plain `Lock` was never
    nested (`append`, `replace`, `remove` and their helpers take it once; `get`
    and `_iter_raw` take none), so the swap loses nothing. `TailIndex` is the
    job log's position (inode+dev, shrink) and, with `verify_bytes`, the
    ledger's head/tail byte check; `accept_own_append` replaces the ledger's
    "index the record I just wrote" shortcut. No fsync added or removed (still
    flush only). Bytes unchanged: the same job-log and ledger scenario (5
    appends, a torn tail, a supersession; brain submit/transition across a
    torn tail) run at HEAD and now gives identical SHA-256 for `jobs.jsonl` and
    `memory.jsonl`. `tests/test_jsonl_store.py` (new, 26 tests): one lock per
    resolved path (symlink, `..`), reentrant, exclusion; 8 threads x 60 20 kB
    lines and 3 processes x 25 interleave nothing; torn-tail repair, exact
    bytes; `TailIndex` incremental, torn tail held, truncate, replace, delete,
    same-length rewrite (only `verify_bytes` sees it), `accept_own_append`.
    The 48 test files naming these modules + layering: 1015 passed (199 of
    them the brain/ledger/candidate set, 199 before and after); `-m slow` on
    those files 8 passed. `tests/test_brain_jobs.py` reads `index.tail.offset`
    now (was `index.offset`).
  - Decision: `models/candidate_store`'s `.lock` flock (a fourth) is left alone:
    it is a sentinel file in a directory of JSON documents, no JSONL and no
    in-process lock, so `jsonl_store` is not its home, and routing it through
    `file_lock` would only add an RLock it has no use for.
  - Durable (fsync) atomic writes are R23.13; this is the append half.
    Pinned by `tests/test_jsonl_store.py`, `test_brain_jobs.py` and the
    ledger tests.

- [x] **R24.10 One JSON-safe converter.** Nine functions turn numpy,
  dataclass and pydantic values into JSON-safe ones: `_jsonable` in
  `tools/plant_validation.py`, `fuel_plant_check.py`, `manifold_check.py`,
  `replay_event_diff.py` and `src/validation/scenarios.py`; `_json_default`
  in `scripts/lambda_delivery_evidence.py` and `analysis/digest.py`;
  `artifact_lineage.json_default`; and `fuel_comp_report.jsonable`, the
  complete one. They drift: one rounds to six places and handles dataclasses,
  one rounds and handles `model_dump`, one does neither, one has no numpy
  handling. The same value serializes differently across the reports in
  `data/analyses/`.
  - [x] `src/jsonable.py`: `jsonable(value, *, ndigits=None)`, generalizing
    the `fuel_comp_report` one. Snapshot each tool's JSON before the swap;
    rounding is the per-caller behavior to keep.
    Done: `jsonable(value, *, ndigits=None, stringify=False)` plus
    `json_default` (the `json.dumps(default=)` form, `stringify=True`), layer 0
    in `tests/test_layering.py`. Nine copies deleted, plus two found by grep:
    `validation/report._default` and `analysis/workbench._clean` (scalar copy,
    now `jsonable`). `rom_effects._plain` stays, by name in the guard's
    exemptions: dataclass-only, strict, and NaN-preserving, where `jsonable`
    would write `null`. Callers: `ndigits=6` for `plant_validation` and
    `fuel_plant_check`; exact for `manifold_check`, `scenarios`,
    `fuel_comp_report` and `workbench`; `jsonable(report)` with no `default`
    for `audit_artifact_lineage` (an unknown type is still refused);
    `json_default` for `digest`, `lambda_delivery_evidence` and
    `validation/report`; `replay_event_diff` loads `src/jsonable.py` by path
    from its own checkout (not from `--tree`, so `ab --before-tree` an older
    rev needs nothing added) with `stringify=True`.
    Evidence: a 33-element corpus (numpy scalars/arrays, `np.bool_`, NaN/inf,
    dataclass, pydantic, datetime/Timestamp/NaT, Path, Enum, set, bytes,
    DataFrame) serialized by each old function at HEAD and by the new one.
    Rounding and plain values are identical in every caller; it is NOT
    byte-identical everywhere. Intended differences: inf/-inf/NaN become
    `null` (plant_validation/fuel_plant_check/manifold_check wrote
    `Infinity`; replay_event_diff and scenarios wrote `NaN`); a type an old copy
    raised on (numpy arrays/`np.bool_`, dataclass, pydantic, set, datetime,
    Path, Enum, bytes, DataFrame) now converts; sets sort; digest and
    lambda_delivery_evidence no longer `str()` numpy scalars and containers
    (`np.int64(7)` was `"7"`, now `7`; datetimes are ISO with `T`), and
    `np.float32(0.1)` widens to `0.10000000149011612` where HEAD wrote the
    string `"0.1"`; replay_event_diff `np.int64` 7 -> `7` (was `7.0`),
    `np.bool_` -> `true` (was `1.0`), NaN -> `null`, so a stored pre-change
    `before.json` compared with a new run shows NaN-vs-null differences in
    score messages and counters. `np.timedelta64` and `np.datetime64` are
    handled before the integer branch (`timedelta64` is an `np.signedinteger`),
    to seconds and ISO, NaT to `null`.
    `manifold_check standing|study` stdout byte-identical against a HEAD
    worktree. `tests/test_jsonable.py` guard: no module-level converter by
    those names under `src/`, `tools/`, `scripts/`, beyond the named exemption.
    Gate: ruff, format, mypy clean; targeted pytest green (see report).

## Looked at and fine

- `signals/pairing` and `analysis/compare` remain the only pairing code on
  the tuning path (R11 held).
- `src/coerce.py` is used; two local `optional_str` copies are listed in
  R27.12.
- `artifact_hash.sha256_file` is the only file hasher.

## Order

R24.8 first: it is a test file, it changes no code, and it stops the regrowth
the other items remove. Then R24.3 (it has measured costs behind it) and
R24.1 with R29.3. R24.6 whenever someone can look at the cells that move.
R24.2, R24.4, R24.5, R24.9 and R24.10 are mechanical and independent.

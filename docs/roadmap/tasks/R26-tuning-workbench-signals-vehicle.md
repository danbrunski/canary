# R26 — Tuning workbench, signals and vehicle records

Part of the 2026-11-12 review ([R22](R22-refactor-README.md)). Scope: the
tuning and workbench half of `src/analysis/` (`tuning*`, `workbench`,
`channels`, `digest`, `lambda_delivery`, `fuel_comp_*`, `operating_context`,
`refit`, `recipes/`, `gps_tracker`, events), `src/signals/`, `src/evidence/`,
`src/vehicle/`. Structure, duplication and efficiency only. Counts are review
snapshots; locate code by symbol. Nothing here has been implemented. Shared
acceptance rules are in R22.

**State of the area.** R05, R11, R16 and R17 held. There is one session
reader (`channels.read_channels` → `loader.read_session`) and one pairing
module; the tuning pipeline does not bypass `compare_channels`. No
`iterrows` or `apply(axis=1)` on session frames. No Xterra constants in shared
code: stoich has no float default anywhere (R05.1), and displacement, ratios
and tire come from `VehicleParameters`. No session is ordered by directory
name. What is left is second-order: a read pattern the digest already paid to
learn, a bin rule that is one rule in name only, a silent-zero in the digest,
and files with clean seams.

The cross-cutting copies found here (`_aware`, `_iso`,
`model_validate(load_yaml(…))`, the bin rule, time-lookup leftovers) are in
[R24](R24-shared-primitives.md).

- [~] **R26.1 The workbench reads each session once per channel.**
  `workbench._read_optional` loops
  `for spec in dict.fromkeys(specs): read_channels(session, [spec], …)`, one
  scan of the parquet chunks per spec, so that one unresolvable name does not
  fail the rest. `tuning_digest._read_specs` records what that costs: "a
  24-minute capture went from seconds to 228 s". `shared_session_read` exists
  for this and `workbench.py` never enters one. `trim_surface` does it twice
  per session (five reads, then seven, then one for baro: about thirteen
  scans per session, times the sessions selected), in the workbench child
  behind the heaviest dashboard page.
  - [~] Time `trim_surface` on one 20-minute tuning session with
    `SharedSessionRead.disk_reads` counting. Laptop done; the Pi figure is not
    measured (needs the Pi, and no analysis runs on it while it collects).
    Session `_capture-tuning-2026-11-12T034249` (20.7 min, 1.78 M rows, 34.5 MB),
    scans counted by wrapping `channels.read_session`, one process, laptop
    under other sessions' load (so seconds are indicative, scans are exact):
    `trim_surface` 13 scans / 41.0 s before, 1 scan / 6.6 s after;
    `timing_surface` 4 / 11.6 s to 2 / 8.4 s (values, then the barometer in
    `conditions_block`); `session_frame` 6 / 16.6 s to 1 / 5.9 s. The result of
    each, `json`-dumped, is identical before and after (HEAD vs this tree, same
    inputs). Script: `<scratchpad>/r261/bench.py`.
  - [x] Wrap each per-session body in
    `with shared_session_read(session, specs, …)` (no behavior change), or
    resolve spec names against the catalog first and make one call.
    Chose the wrap: `read_channels` still runs once per spec, so a name that
    does not resolve (or whose rows refuse it, e.g. `_did_series` on pre-K01.1
    rows, which is decided from the data) fails alone exactly as before; a
    one-call read would fail the whole batch and need that isolation rebuilt.
    `_read_optional` itself enters the block (every caller: `_aligned_or_missing`
    for `trim_surface`, `timing_surface`, `pull_comparison`, the barometer, the
    regime channels), and `session_frame` and `trim_surface` enter an outer one
    that the inner ones reuse. `tests/test_workbench_analysis.py::
    test_read_optional_reads_the_session_once_and_isolates_each_spec` (an
    unresolvable, an absent and present specs: 4 scans in the old loop, 1 now,
    frame and `unresolved` equal to the old loop's),
    `test_session_frame_and_value_surfaces_read_each_session_once`.
  - [x] `trim_surface`: derive "missing" from what the first read returned and
    drop the second. One `_read_optional` of all seven specs; `_not_captured`
    names the missing from the required five (the optional bank 2 never
    appears in `missing`), and the barometer read for `conditions_block` is
    served from the same scan (`baro_reads`).
    `test_trim_surface_reads_each_session_once_including_the_barometer`
    (two sessions, two scans) and
    `test_trim_surface_not_captured_keeps_missing_order_and_reads_once`.
  - Must not change: `Unavailable.missing` order, the `not_captured` /
    `refused` split. Held: `test_trim_surface_not_captured_keeps_missing_order_and_reads_once`
    compares against the old `_aligned_or_missing` logic kept in the test with
    one spec unresolvable, one resolvable but absent, the rest present
    (`missing` = required order, the unresolvable one in its place). The
    `refused` kind is `_session_delivery`'s and untouched. Gate:
    `pytest -m ""` over every test file naming the workbench modules or
    `shared_session_read`, ruff, mypy green.
  - Verified independently: byte-identical JSON (HEAD vs tree) for
    `trim_surface`, `timing_surface`, `session_frame` and `pull_comparison`
    on five real sessions (current and old DID-only tuning captures, a default
    session); `pull_comparison` over two sessions 8 → 4 scans. Cost: one
    session's union frame is held at once, so peak RSS rises
    (laptop, 20.7-min capture: `trim_surface` 296 → 310 MB, `timing_surface`
    276 → 335 MB, two-session `pull_comparison` 273 → 398 MB). The frame is
    freed per session, so a multi-session selection does not stack it; the
    workbench child's `RLIMIT_AS` is 4 GiB on the 8 GB Pi. A session several
    times larger than 43 MB on disk was not measured against that cap.

- [x] **R26.2 The digest reports a failed read as "none measured".**
  `tuning_digest.py` has eleven `except Exception`. Three of them turn an
  exception into a count: `_infer_wot` (`except Exception: measured = 0`),
  the Mode 01 branch of `_wot_inventory` (`paired = 0`) and the tagged-pull
  branch (`pass`, then falls through to inference). Those counts feed the note
  "WOT seconds were captured but none carry both a measured and a commanded
  AFR … delivery error cannot be computed". A read that raised on a bad spec
  or a corrupt chunk is then stated as a fact about the drive. This is
  `AGENTS.md`'s "an empty result must say which kind of empty it is".
  - [x] Catch the set `workbench._read_optional` catches
    (`ChannelSpecError`, `ValueError`, `KeyError`); carry the cause in
    `WotInventory.reason` (the field exists) and log it with the session name.
    Done in `tuning_digest.py`: `_READ_FAILURES` is that set; the three blocks
    now catch only it, log `log.warning(... session_dir.name ...)`, and put
    "could not read <specs>: <cause>" in `reason`. New `WotInventory.read_failed`
    (also in `to_dict`) marks a count as unknown, not zero; `_section_wot`
    then emits "WOT inventory is incomplete — a read failed: <cause>" instead
    of the "none carry both ... delivery error cannot be computed" note. A
    failed tagged read falls through to inference as before but the result
    carries `read_failed` and says the tags could not be used (the tagged
    `try` now wraps only the `read_channels` call). Anything outside the set
    propagates to `digest._tuning_section`, which already turns it into
    `{"available": False, "kind": "failed", "error": ...}` (MemoryError
    re-raised). Left alone, as the item allows: the two first-read
    `except Exception` blocks (`_infer_wot`, Mode 01) already return
    `unavailable` with the cause and no count.
  - [x] Test: make `read_aligned` raise and assert the digest says it could
    not read, not that no pair exists. `tests/test_tuning_digest.py`: Mode 01
    pair read and tagged read through `compute_tuning_digest`; the inferred
    measured-AFR read through `_infer_wot` directly (the digest never reaches
    it on the Xterra DID profile, whose measured AFR is refused for want of a
    baro); each paired with a test that a `RuntimeError`/`TypeError` still
    propagates. `pytest tests/test_tuning_digest.py -n0`: 27 passed; the other
    `tuning_digest` test files: 89 passed.
  - Same shape found, not changed (follow-up): `tuning_digest.py` baro read
    in `_measured_afr_summary` (`baro_long = None` is then reported as
    "baro_missing"), the bank read there (`continue`, bank silently absent),
    `_did_pull_inventory` RPM read ("RPM provider could not be read", no
    cause) and the per-spec read fallback in `_read_specs`. Also
    `_coverage_notes`, which prints "{n} WOT seconds, {with_afr_pair}
    carrying an AFR pair" without checking `read_failed` (probably
    unreachable when the same read fails, but it is the same shape; found by
    the verifier).

- [x] **R26.3 The catalogs are validated again on every `read_channels`.**
  `channels.py` builds a `_Catalogs` per `raw_read_filters`, `read_channels`
  and `rescale_channel_rows` call. `decode/bindings.load_bindings` has no
  cache; `decode/dids.load_did_map` caches the parse and re-validates. Each
  measured about 10 ms on the laptop (pydantic over `data/dids.yaml`,
  296 kB). `read_channels` has 39 call sites; with R26.1 that is a quarter of
  a second of validation per session.
  - [x] A module cache keyed by `(path, mtime_ns, size)` that `_Catalogs`
    shares. `load_did_map`'s docstring says each caller gets its own model to
    mutate: find out whether any caller does, and return a copy only if so.
    Evidence: `load_did_map` and `load_bindings` both go through
    `src.yaml_load.load_yaml_model` (R24.3), so `_Catalogs` (unchanged) shares
    one cached model per file; `dids._parsed_yaml` removed. No caller mutates
    the returned models: a static scan of every `load_did_map`/`load_bindings`
    caller in `src`, `scripts`, `tools` (the `model_copy(update=...)` sites
    build new models), and the verifier's own AST scan agrees. A scratch pytest
    plugin (not committed) that snapshotted each cached model's JSON and
    compared on every hit also logged no drift across a 5923-test sweep of every
    test file mentioning the loaders (1 unrelated failure, in
    `test_plant_validation_ecu_outputs`). So no copy is returned.
    `tests/test_yaml_load_model.py::test_an_edited_dids_yaml_is_picked_up_by_the_next_read_channels`
    (edit in `tmp_path` seen by the next `read_channels` and `raw_read_filters`).
    Measured on the laptop (median of 40 calls, hand-built 3-row frame with one
    DID row, not a session; ratios, not Pi numbers), command
    `.venv/bin/python <scratchpad>/bench.py` (loads `data/dids.yaml` and
    `data/bindings.yaml`, times `load_did_map`, `load_bindings`,
    `read_channels(frame, ["did:ECM_COOLANT_TEMP"])`, the same plus
    `decoded:ECM_COOLANT_TEMP`, and `raw_read_filters` on both specs):

    | call | before | after |
    |---|---|---|
    | `load_did_map` | 6.71 ms | 0.04 ms |
    | `load_bindings` | 16.0 ms | 0.04 ms |
    | `raw_read_filters` (2 specs) | 26.0 ms | 0.15 ms |
    | `read_channels` (1 did spec) | 24.2 ms | 13.9 ms |
    | `read_channels` (did + decoded) | 53.7 ms | 31.8 ms |

    Absolute numbers vary with laptop load; the ratio is what reproduced. The
    verifier's re-run measured `load_did_map` 1.99 to 0.01 ms, `load_bindings`
    4.41 to 0.01, `raw_read_filters` 6.94 to 0.04, `read_channels`
    5.71 to 3.67 and 14.43 to 6.63. The remaining `read_channels` cost is the
    frame work, not the catalogs.
    The first call after a file change still pays the parse and validation.

- [x] **R26.4 `vehicle/profiles.py` (1,724 lines) is six modules.** By line
  range: schema and naming; registry file I/O and overlay merge;
  edit / delete / restore with their evidence rules; active-profile resolution
  from the VIN probe; path scoping (`scope_config`, `profile_data_paths`,
  `operational_file`); legacy-calibration migration. The flash-history
  accessors (`active_registry`, `active_flash_history`,
  `_flash_history_cache`) read `tune_revisions.yaml` and belong with
  `tune_revisions.py`, which does not import `profiles`: the three
  function-level imports hide no cycle. `active_flash_history` is on the
  per-tick feature path (`features/live.py`) and re-reads and hashes both
  registry files each tick.
  - [x] A `src/vehicle/profiles/` package (`model`, `edit`, `resolve`,
    `scope`, `legacy`) whose `__init__` re-exports every name now imported
    (107 import sites). Flash history moves to `tune_revisions.py` or
    `flash_history.py`.
    `src/vehicle/profiles.py` is gone; `profiles/model.py` (338 lines: schema,
    naming, registry I/O and overlay merge), `edit.py` (586: edit, revert,
    delete, restore, the evidence rules, and `ProfileResolution` with its
    process record, which the rules read and `resolve` writes), `scope.py`
    (201: `scope_config`, `profile_data_paths`, the profile data locations),
    `legacy.py` (216: adoption and migration of the pre-R05.15 directory),
    `resolve.py` (363: the VIN-probe resolution), and `__init__.py` (227:
    re-exports of every public name the module had, 64 names). Imports run one
    way: `model` < `scope`, `edit` < `legacy` < `resolve`
    (`test_a_profiles_module_imports_only_below_itself`). One placement differs
    from the item's list: `operational_file` is in `legacy`, not `scope`. It falls
    back to `legacy_operational_dir`, and `legacy` needs `scope`'s `operational_dir`
    and `source_registry_path`, so the other way round is a cycle. The flash history is the new `src/vehicle/flash_history.py`
    (`active_registry`, `active_flash_history`, `_flash_history_cache`, and with
    them `active_registry_path` and `active_registry_sources`, which only name the
    files it reads); its three function-level imports are module-level there.
    `tune_revisions.py` itself is untouched: putting the accessors in it would
    make it import `profiles`, which imports it.
    **Moved code is body-identical.** `<scratchpad>/r264/ast_proof.py` compares
    `ast.dump` of every top-level definition in `git show HEAD:src/vehicle/profiles.py`
    against the new modules: 97 names, 97 present once, 95 identical, and the 2
    others (`active_registry`, `active_flash_history`) identical once their three
    hoisted function-level imports are removed. Nothing missing, nothing extra.
    **Re-exports.** `src.vehicle.profiles` re-exports all 64 public names (the 51
    any file imports, plus the rest of the old surface); the four flash-history
    names (`active_registry`, `active_registry_path`, `active_registry_sources`,
    `active_flash_history`) are served by a module `__getattr__` (PEP 562, with
    `TYPE_CHECKING` imports so mypy sees the real types): `flash_history` imports
    `profiles.legacy` and `profiles.scope`, which run the package `__init__`
    first, so an eager import there fails whichever of the two is imported first.
    `test_each_module_imports_first_in_a_fresh_interpreter` imports each of the
    seven modules first in a clean interpreter; `import src.vehicle.profiles`
    still loads no pandas. Private names are not re-exported: the one caller that
    used one (`tests/test_profiles.py`, `_scope_file`) imports it from
    `src.vehicle.profiles.scope`.
    **Monkeypatch traps: none to repoint.** No test or script patched an attribute
    of `src.vehicle.profiles` (`monkeypatch.setattr(<profiles alias>, ...)`,
    `"src.vehicle.profiles.X"` strings, `patch.object`, direct assignment): the one
    module alias, `profiles_mod` in `tests/test_profiles.py`, was used only to read
    `_scope_file`. `test_nothing_patches_a_name_the_package_only_re_exports` (an
    `ast` walk of `src`, `scripts`, `tools`, `tests`, with
    `test_the_tripwire_for_a_patch_through_the_package_can_fail`) keeps it so.
    **Pickles and caches.** Nothing persists `src.vehicle.profiles.<Class>` names:
    the IF artifact payload holds sklearn models and dicts, and the score cache
    (`src/models/evaluation_cache.py`) is keyed by a hash of every `src/**/*.py`
    path and byte, so any move already invalidates it. The two things pickled
    across a process boundary, `CanaryConfig._scope_source` (a `VehicleProfile`, to
    the scoring child) and the `ProfileEditError` family, now pickle by reference to
    `src.vehicle.profiles.model` / `.edit`
    (`test_the_classes_that_cross_a_process_boundary_pickle_from_their_new_modules`;
    `tests/test_exceptions_pickle.py` still holds every class to it). Logger names
    are now `src.vehicle.profiles.resolve` and `.legacy` (children of the old name,
    so `caplog.at_level(logger="src.vehicle.profiles")` still reaches them).
  - [x] `active_flash_history` per tick: R29.9's memo already did the work that
    mattered, so no second memo was added. `load_tune_revisions` now caches the
    parse by file identity (`load_yaml_model`, with its racy-window rule), so
    what each call still does is `active_registry_sources` (two `exists`) and one
    `read_bytes` + sha256 per file to build the bytes key. Measured at HEAD, median
    of 300 calls on the laptop, Xterra scoped config: 0.034 ms with the 10 kB
    tracked registry alone, 0.036 ms with an operational copy beside it (parse
    memo alone, 0.038 ms); the same after the move (0.034 and 0.035 ms,
    `<scratchpad>/r264/bench_flash.py`). A stat-keyed memo in front of that would reintroduce
    the coarse-mtime stale read the bytes key exists for
    (`test_an_edited_flash_history_is_reread`), to save ~30 microseconds per tick.
  - [x] Must not change: `scope_config`'s output, the registry file format, the
    bytes-keyed flash-history cache
    (`test_an_edited_flash_history_is_reread`).
    `tests/fixtures/scope_config_golden.json` was generated by
    `tests/test_profiles_split.py::scope_snapshot` run against the unsplit
    module (HEAD `2f83f934`): for each of the four tracked example configs and
    each of the two profiles in the tracked `profiles.yaml` (2008 and 2013
    Xterra) plus a synthetic profile with no year or VIN, every dotted field of
    the whole config that scoping changes (25 paths, nothing removed), the
    `_scope_source` identity, idempotence, the re-scope refusal text, and
    `rescope_like`. `test_scope_config_returns_what_it_returned_before_the_split`
    compares the split module field by field (12 entries, passed);
    `test_the_vehicle_block_is_the_profiles_own` holds the `vehicle:` block, which
    follows `profiles.yaml` edits, to the property instead. The registry format
    is untouched (`REGISTRY_SCHEMA_VERSION`, `save_registry`, `load_registry` are
    in the AST proof; `tests/test_profiles.py`, `test_profile_edit.py`,
    `test_profile_delete.py`, `test_operational_calibration_scope.py` pass).
    `test_an_edited_flash_history_is_reread` passes on the moved cache.
    Gate: `ruff check .`, `ruff format --check .`, bare `mypy` clean; `pytest -m ""
    -n 4` over the 96 test files that import `src.vehicle.profiles` or mention
    `tune_revisions`, `flash_history` or `active_registry`, plus
    `test_layering.py`, `test_state_dir.py`, `test_config_golden.py`: 2485
    passed, 1 xfailed, 1 failed
    (`tests/test_exceptions_pickle.py::test_the_bases_reach_the_classes_that_were_hand_written`,
    which names the R29.7 agent's untracked `TuningStorageSwitchError` in
    `src/control/tuning_mode.py`, not a profiles class).
    `src/vehicle/` is in `scripts/ml_paths.txt`: the push gate adds the
    `ml_training` tier for this change.

- [ ] **R26.5 `signals/afr.py` (1,513 lines) is five modules, and its error
  surface is used only by its own tests.** Calibration schema; baro samples;
  sensor-to-AFR transform; target lookup (about 350 lines); frame appenders.
  `afr_error_surface` has no caller in `src/`, `scripts/` or `tools/` — four
  tests in `tests/test_afr.py` and nothing else — and bins rpm × load its own
  way in a layer-0 module. The production surface is `workbench._cells`.
  `signals/derived.py` has the same shape on a smaller scale: `TripSummary`,
  `summarize_trip` and `_accel_metrics` are a per-trip report, not a signal
  formula.
  - [ ] Operator question: `docs/roadmap/capture-profiles.md` still lists the
    AFR error surface as "software complete; data blocked". Keep it for that,
    or delete it with its tests? (The `engine-management` skill's
    `canary-workflow.md` also points readers at it; see L01.23.)
  - [ ] `signals/afr/` package behind a re-exporting `__init__`
    (`calibration`, `baro`, `transfer`, `target`, `flash_history`, `frame`).
  - [ ] Trip summary to `signals/trip.py` or `analysis/`.

- [ ] **R26.6 `signals/fsm.py` mixes serve-time limits with a PDF scraper.**
  `PhysicalLimit` and `PHYSICAL_LIMITS` are imported by
  `preprocessing/derived_store.py` and `plausibility/limits.py`. The other
  ~400 lines (`clean_param_name`, `extract_param_names`,
  `extract_pdf_section*`, `classify_spec`, `parse_ranges`) are used only by
  `tools/extract_fsm_signals.py` and `tests/test_fsm.py`.
  `tests/test_runtime_dependencies.py` exists because pdfplumber once
  crash-looped the daemon through this module.
  - [ ] Move the extraction helpers to `signals/fsm_extract.py` or into the
    tool, so "no pdfplumber at serve time" is structural rather than tested.

- [x] **R26.7 `analysis/digest.py` (1,056 lines) is a memory-admission gate
  plus digest compute.** About 450 lines (`DigestSkipped`, `DigestBudgets`,
  the three `*_budget` functions, `digest_preflight`,
  `write_digest_skipped`, `_ranked_start`, …) decide whether a digest may run;
  the rest computes and persists one. They meet at one call.
  - [x] `analysis/digest_admission.py`, re-exported from `digest.py` until
    callers move. D03.63 touched this region recently; go after it.
    `digest_admission.py` (600 lines) holds the budgets, `DigestBudgets`,
    `digest_preflight`, `DigestSkipped`, `write_digest_skipped`,
    `clear_digest_skipped` and the top-level lock and rank rules
    (`_TOP_LEVEL_LOCK`, `_ranked_start`, `_top_level_may_take`, ...), 35
    top-level definitions moved verbatim. `digest.py` is 552 lines and imports
    them; `SCHEMA_VERSION` moved with `DigestSkipped`, which defaults to it.
    `ast.dump` of all 55 top-level definitions of HEAD's `digest.py` against
    the two modules: 55 identical, 0 missing, 0 extra (only `log` is written
    twice: the admission module's is `getLogger("src.analysis.digest")`, so the
    refusal warning keeps the logger name the Pi's logs address). One
    `_TOP_LEVEL_LOCK` object, shared by `write_digest` and
    `write_digest_skipped`. Repointed `src/control/trip_end.py`'s import of the
    private `_MIB` to `digest_admission`; every public name and
    test-imported name still imports from `digest` (`__all__` gained
    `REASON_INTERRUPTED_AT_SHUTDOWN` and `TOP_LEVEL_RANK_KEY` so mypy sees the
    re-export). No test patched a moved global of `digest`.
    `tests/test_analysis_module_splits.py` pins the re-exports to the owner's
    objects, the logger name and the single lock. Tests importing either
    module (37 files, plus `test_layering`, `test_source_rules`,
    `test_cold_start_imports`, and the evidence/droop files below): HEAD
    1,103 passed 3 skipped, HEAD plus this change 1,103 passed 3 skipped.

- [~] **R26.8 `analysis/channels.py` (1,414 lines): reading and alignment
  separate cleanly.** Read plan and narrowing (`raw_read_filters`,
  `SharedSessionRead`); series builders and `read_channels`; alignment
  (`read_aligned`, `align_long`, `as_of_*`), which is pure frame code and is
  most of what `workbench` and `tuning` import. `align_long` also filters
  `long[long["spec"] == name]` once per spec; one `groupby("spec")` does it in
  a pass.
  - [x] `channels_align.py` and `channels_plan.py`, re-exported. After R26.1
    and R26.3, which touch the same functions.
    Split by dependency, not only by the three groups named above. `channels.py`
    is 500 lines: the series builders, `read_channels`, `read_aligned`,
    `rescale_channel_rows`. `channels_plan.py` (797 lines, 42 definitions) is
    everything that decides what to read and reads it: the spec grammar and
    catalogs, `raw_read_filters` and narrowing, `SharedSessionRead`, the
    acquisition clock and `read_acquisition_frame`. The grammar and the clock
    had to go with it because `SharedSessionRead` and `parse_spec` sit under
    both halves; otherwise `channels_align` and `channels_plan` would import
    each other. `channels_align.py` (232 lines, 10 definitions) is the pure
    frame code: `AS_OF_ATTR`, `as_of_validity`, `as_of_refusal`,
    `refuse_as_of`, `concat_channel_frames`, `align_long`. `read_aligned` stays
    in `channels.py` because it calls `read_channels`; moving it would make
    `channels_align` import `channels`, which re-exports it. Every public name
    imports from `channels` unchanged (`__all__` of the 23 re-exports).
    `ast.dump` of all 65 top-level definitions of HEAD's `channels.py` against
    the three modules: 65 identical, 0 missing, 0 extra, none defined twice.
    Facade hazard, swept with `ast` over src, scripts, tools, tests and
    data/: no string patch target; module-attribute uses were
    `channels.read_session` (three tests), `channels._on_acquisition_clock` and
    `channels._Narrowing` (`tests/test_channels_raw_read.py`). Every
    `read_session` call now lives in `channels_plan`, so those tests
    (`test_channels_raw_read.py`, `test_tuning_digest.py`,
    `test_workbench_analysis.py`) patch `channels_plan` (one target, and
    `test_every_session_read_of_channels_goes_through_one_module` keeps it so).
    The patches of `channels._raw_read_filters`, `_read_columns` and
    `_narrowing` stay on `channels`: `read_channels`, their only caller on
    those paths, is still there. `compare_evidence` and `delivery_evidence`
    hash `channels.py` into `code_version`; they now hash `channels_plan.py`
    and `channels_align.py` too, or an edit to the reader would not change the
    evidence's code version. Tests importing either module (same set as
    R26.7): HEAD 1,103 passed 3 skipped, HEAD plus this change 1,103 passed 3
    skipped. `ruff check .`, `ruff format --check .` and bare `mypy` clean;
    `test_layering` green.
    Not done: the `align_long` filter `long[long["spec"] == name]` once per spec
    (one `groupby("spec")`). That is a change of code, not a move, and would
    break the verbatim comparison above; it is the open sub-item below.
  - [ ] `align_long` (`channels_align.py`): one `groupby("spec")` in place of
    the per-spec `long[long["spec"] == name]` filter, with a test that the
    aligned output is identical.

- [ ] **R26.9 `GpsTracker` is daemon state that lives in `analysis/`.**
  733 lines; `observe` runs per telemetry record and `persist` fsyncs. It is
  imported by `daemon/components.py`, three dashboard modules and
  `control/trip_end.py`.
  - [ ] Use the durable helper from R23.13 in `persist`. Moving the class out
    of `analysis/` needs a layer decision in `tests/test_layering.py`; do it
    only if something else forces it.

- [ ] **R26.10 Per-row Python loops on session frames.** Mostly cold; listed
  so they are found once.
  - `signals/committees.speed_accel_g`: a scalar loop over every row, run by
    `append_committee_member_transforms` in the feature builder (a short
    window live, whole sessions in training). Vectorize with `searchsorted`;
    the hole semantics are pinned by `tests/test_committees*`.
  - `analysis/events/idle_period.py` and `events/stop.py`: per-element
    `.iloc` in a `range(len(…))` loop. Run-length edges from `np.diff`.
  - `analysis/session_deviations.py`: scalar `bin_index` per sample, which
    builds a one-element array each time.

- [x] **R26.11 Dead symbols.** Each checked by word-grep over `src`,
  `scripts`, `tools`, `tests` and `docs`:
  `analysis/refit.py` `_validation_r_squared`;
  `analysis/surface_2d.py` `_clean_xyz`;
  `analysis/fuel_comp_samples.py` `_separation`;
  `signals/fsm.py` `extract_pdf_section` (superseded by
  `extract_pdf_section_with_specs`).
  *Done 2026-11-13.* Re-grepped (`grep -rnw`) over `src scripts tools tests
  docs data` and `src/dashboard/static`: each name occurred only at its own
  `def` (and in this item); `extract_pdf_section_with_specs` does not match
  `-w`. No test, no string reference (`getattr`, registry, YAML). All four
  deleted, none had a test to take with it. `src/analysis/fuel_comp_samples.py`
  `_separation` also names an unrelated `self._separation` in
  `collection/hybrid_cycle.py`, untouched. The `extract_pdf_section_with_specs`
  docstring said "Like `extract_pdf_section`"; reworded. `pdfplumber` stays a
  function-level import there. `pytest tests/test_refit.py test_surface_2d.py
  test_fuel_comp_samples.py test_fsm.py` green; `ruff`, `mypy` clean.

- [ ] **R26.12 Large but cohesive; split only when next touched.**
  `evidence/ledger.py` `MemoryLedger` (635 lines, 28 methods): the three
  `_check_*` write rules and `assess` could become functions over an index in
  `evidence/outcome_rules.py`. `analysis/operating_context.py` (1,366): the
  pydantic declaration schema (~230 lines) could leave the evaluator.

## Looked at and fine

- `cross_reference.py` and `broadcast_correlate.py` hand-roll nearest-in-
  tolerance pairing and rank by r². `docs/analysis-pairing.md` evaluated and
  declined to change them (a different storage format; `broadcast_discovery`
  is the successor). If either is ever trusted for a binding, route it through
  `match_ordered` and `scale_stability` first.
- `tuning.compare_sessions` compares session against session per cell with a
  regime-keyed noise floor. That is not a two-signal aggregate comparison.
- `recipes/shared.py` `shared_recipes` (283 lines) is a table of
  declarations. R05.7's "trailing loop overwrites minimums" did not regrow.
- `MemoryLedger._refresh` is incremental and indexed; it is not a
  `JsonlStore` duplicate.
- Per-vehicle recipe and signal modules loaded by slug are an operator
  decision (2026-09-25).

## Not reviewed

`capture_coverage.py` (another session's uncommitted edits),
`kline_exchanges`, `known_cases`, `memory_verification`, `did_scan`,
`hypothesis_tracking`, `training_disposition`, `fingerprint`, `llm_assist/`,
`dyno_*` beyond one grep, most of `events/`, `vehicle/{vin,vehicle_ledger,
platforms,interventions}.py`.

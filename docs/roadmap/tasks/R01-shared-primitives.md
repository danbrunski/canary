# R01 — shared primitives: one home for the rules everyone re-derives

Opened 2026-09-24 from the refactor review (see
[R00](R00-refactor-README.md)). Every item here is a rule that four or more
modules implement for themselves. The copies have already diverged, and later
R-files build on these homes, so this file goes first. Same conventions as
the other task files: `[x]` only with evidence, `[~]` partial with the rest
named.

## Rules

- **One definition, and callers can't avoid it.** A new home is done when
  the old copies are *deleted* or re-export it, not when it exists beside them
  ("make it impossible, not documented").
- A behaviour change hidden in a dedupe (a copy that let `inf` through now
  refuses it) is named in the item's evidence, with the caller that sees it.
- Serve-path changes (anything imported by `src/daemon/`, `src/collection/`
  or `src/preprocessing/live.py`) replay a pinned session before and after,
  and diff the events and rows.

---

- [x] **R01.1 One session-name module: kind, start time, ordering.**
  *Drift bug.* Three things answer "what is this directory?", and they
  disagree:
  - The regex `^\d{4}-\d{2}-\d{2}T\d{6}$` is copied in `control/sessions.py`,
    `control/classification.py`, `control/trends.py`,
    `control/replay_control.py`, `storage/catalog.py` and
    `collection/tags.py`. `storage/retention.py` inlines
    `strptime("%Y-%m-%dT%H%M%S")` six times, and `datasets/health`
    `_session_started_date` does it again.
  - There are three start-time parsers. `sessions.session_started_at`,
    `sessions._parse_session_started_at` (captures only) and
    `catalog._parse_started_at` all differ. The catalog one accepts
    `_replay-`/`_tuning-` but not `_capture-`, so catalog rows for digested or
    pinned captures get `started_at = NULL`.
  - `is_session_dir_name` matches plain and `_capture-` names only (verified
    2026-09-24). `parquet_writer.enter_tuning_mode` creates `_tuning-<id>`,
    and every HTTP read of such a session 404s.
  - `collection/tags.py::find_latest_session` returns
    `max(candidates, key=lambda d: d.name)` over plain names only (verified).
    So `scripts/tag.py` with no `--session`, run during a capture, tags the
    *previous* session. That is the "which session is live?" conflation
    AGENTS.md names.
  - `sessions.list_sessions` sorts by `session_id`, the directory name. With
    captures included and `filter_sessions(limit=N)`, "newest N" returns the
    lexically-last profile names. No caller passes `limit` today.
  - `classification._iter_session_dirs` and `trends._iter_session_dirs` are
    AST-identical and name-sorted, and `trends._ordered_fingerprints` depends
    on that order.
  - Scripts re-derive `session_started_at(p) or datetime.min` as a sort key:
    `retrain.py` and `evaluate_plausibility.py`.
    `evaluate_detectors._spread_in_time` sorts by `(session_started_at(n), n)`,
    which raises `TypeError` when an unparseable name sits beside parseable
    ones.

  **Do:** `src/session.py`, which is already the leaf module that owns
  `make_session_dir`'s strftime, gains:
  - `SessionKind` (live / capture / tuning / replay);
  - `classify_session_name(name) -> (kind, started_at) | None`;
  - `session_sort_key(path)`, total over unparseable names;
  - `iter_sessions(root, kinds=...)`, ordered by the parsed time.

  `control.sessions.session_started_at` / `latest_session` /
  `is_session_dir_name` become thin wrappers over them (keep the names; they
  are the sanctioned API). `find_latest_session` delegates to `latest_session`.
  The catalog, retention, trends, classification, replay, tags and
  `datasets/health` call the one parser.

  **Operator decision:** does `_tuning-<id>` count as a session for HTTP reads
  and sidecars? Record the answer here before choosing the `kinds` default.

  **Accept:**
  - Failing tests first:
    - `find_latest_session` during an open `_capture-…` returns the capture;
    - a catalog row for a `_capture-` session has `started_at`;
    - `list_sessions(captures=True)` + `limit` returns the newest by time.
  - `grep -rn 'T\\\\d{6}\|%Y-%m-%dT%H%M%S' src scripts tools` finds only
    `src/session.py`.
  - Size **M**. Tests that pin today's order: `test_tags.py`,
    `test_storage_catalog.py`, the retention and replay tests.
  *Partial 2026-09-25 (implementation done; one operator decision open).*
  `src/session.py` gained `SessionKind`, `classify_session_name`,
  `session_sort_key` (total: unparseable names sort first), `iter_sessions(root,
  kinds=...)` (required `kinds`, ordered by parsed time), `session_kind`,
  `format_session_timestamp`/`parse_session_timestamp` and
  `SESSION_TIMESTAMP_FORMAT`. `control.sessions.session_started_at` /
  `latest_session` / `is_session_dir_name` / `list_sessions` are wrappers;
  catalog, retention, trends, classification, replay, tags, `datasets/health`,
  `mcp_server` → `storage/telemetry_query`, `scripts/_cli`,
  `migrate_to_profiles`, `tools/corpus_report` use the one parser.
  `find_latest_session` returns the newest of live/capture/tuning by parsed
  time (not `latest_session`, which needs a flushed parquet chunk that a
  `record.py` `frames.log` session never has; replay output is excluded:
  a tag is a physical action). Sort keys in `retrain.py`,
  `evaluate_plausibility.py`, `evaluate_detectors.py` (`_spread_in_time`'s
  `TypeError` gone), `workbench_jobs`, `models/evaluation` use
  `session_sort_key`. Failing-first, shown failing on a HEAD worktree and
  passing now: `test_tags.py::test_find_latest_session_returns_an_open_capture`,
  `test_storage_catalog.py::test_a_capture_row_has_started_at`,
  `test_session.py::test_list_sessions_with_captures_and_limit_returns_newest_by_time`
  (HEAD returned `beta, zeta`). The grep returns only `src/session.py` (the
  format constant and the stamp regex). The accept grep missed a dead copy
  (`tune_revisions._SESSION_STAMP`, deleted); the widened form
  `grep -rnE '\\d\{4\}-\\d\{2\}-\\d\{2\}|%Y-%m-%dT%H%M%S' src scripts tools`
  catches it and now returns `src/session.py` plus two anomaly-log *date*
  regexes (`mcp_server/resources.py`, `mcp_server/tools/search.py`), which are
  not session names. `trip_end.trip_block` sorts by `session_sort_key` too
  (its paths are writer-created, so unparseable-first vs last never applies).
  **Behaviour changes:** `classify_session_name` / `session_started_at` /
  `latest_session` are anchored (capture slugs `[a-z0-9-]+`) and return `None`
  on an invalid calendar date instead of raising; a name merely *containing*
  a stamp no longer parses. Callers: `analysis/drives.py`,
  `tune_revisions.py`, `analysis/workbench.py`, `scripts/evaluate_detectors.py`,
  `models/evaluation.py` (the verifier checked 268 real names: no regression).
  `find_latest_session`'s change reaches `scripts/tag.py`,
  `scripts/stop_recording.py`, `scripts/guided_session.py` and
  `scripts/correct_tag.py`. Pinning suites green: `test_tags`,
  `test_storage_catalog`, `test_retention_tick`, `test_replay_*`,
  `test_vehicle_state`, `test_control_trends*`, `test_control_classification`.
  **Remaining: the operator decision below.** Today's behaviour is kept:
  `_tuning-*` is not in `src.session.RECORDED_SESSION_KINDS`, so HTTP reads
  and sidecars still refuse it; either answer is adding or not adding
  `SessionKind.TUNING` to that one constant (pinned by
  `test_session.py::test_tuning_sessions_are_not_http_resolvable_pending_the_operator_decision`).
  **Decided and done 2026-09-25** (operator decision, R00: `_tuning-*` counts
  as a session for HTTP reads and sidecars). `SessionKind.TUNING` joined
  `RECORDED_SESSION_KINDS`, and `list_sessions(captures=True)` lists
  `_tuning-*` beside `_capture-*` (a readable session should be listable).
  Effects checked: `resolve_session`/`is_session_dir_name` accept it, so HTTP
  reads, digest/trip-end preflight, `logs`, `workbench_jobs` and
  `analysis/drives` listings include it; `recorded_sessions` includes it by
  default (`evaluate_detectors`' explicit `| {TUNING}` is now redundant, left
  as is); `latest_session` already answered over every kind; the catalog
  already parsed `_tuning-` start times; classification still refuses
  non-live names (`routes/control/classification.py`, corpus labelling);
  training discovery still excludes it by the `_` prefix. The pending test
  became `test_session.py::test_tuning_sessions_are_http_resolvable`
  (resolve, `recorded_sessions`, `list_sessions(captures=True)`,
  `latest_session`, and `discover_sessions` still excluding it);
  `test_iter_sessions_*` updated. Suites green: `test_session`,
  `test_storage_catalog`, `test_tags`, `test_trip_end_digest`,
  `test_tuning_digest`, `test_parquet_writer`, `test_mcp_parquet_query`,
  `test_evaluate_detectors_script`, `test_evaluate_detectors_default_profile`,
  `test_physical_drives`, `test_mcp_logs`, `test_recorded_sessions`,
  `test_stored_sessions_router`.

- [x] **R01.2 One repo-rooted home for default data paths.**
  *Drift bug.* The same data files have several default paths, some rooted at
  the repo and some at the working directory:
  - `Path("data/dids.yaml")` / `Path("data/bindings.yaml")`, relative to the
    working directory, in `preprocessing/decode.py`, `feature_cache.py`,
    `feature_pipeline.py`, `validation/harness.py`, `validation/evidence.py`
    and `synthetic_bus.py`, plus ~28 `Path("data/…yaml")` defaults in
    `src/analysis/`.
  - Repo-rooted versions exist in `decode/engineering.py` and
    `dashboard/paths.py`. `obd_scheduler._DIDS_YAML_PATH` is a third, and
    `collectors.py` imports it as a private name.
  - `DEFAULT_BINDINGS_PATH` is defined 6 times in `src/`, three rooted at the
    repo and three at the working directory. `--bindings` is declared in 19
    scripts/tools, with both default styles.
  - The concrete failure: `build_feature_frame` loads DIDs from
    `feature_pipeline.DEFAULT_DIDS_PATH` (working-directory-relative, verified
    2026-09-24) and has **no `dids_path` parameter**, although it has one for
    bindings. The feature-cache key hashes a *different* constant,
    `feature_cache.DEFAULT_DIDS_PATH`. A trainer run from another directory
    silently drops every `decoded:`/`did:` feature, and the cache cannot tell.

  **Do:** create `src/paths.py`, with `REPO_ROOT`, `DIDS_YAML`,
  `BINDINGS_YAML`, and the profile-relative helpers if R05.1 does not claim
  them. `dashboard/paths.py` and `decode/engineering.py` re-export from it.
  Give `build_feature_frame` a `dids_path` parameter and thread it into the
  cache key. Move the DID-catalog accessors out of `obd_scheduler` into
  `src/decode/dids.py` (`load_repo_did_map`, `known_did_names`, `did_access`,
  `memory_channels`, `did_response_widths`), re-exported from
  `obd_scheduler`. Retire the documented copy in `control/obd_request.py`.

  **Accept:**
  - A failing test first: `build_feature_frame` run from a `tmp_path` working
    directory produces the same `did:` columns.
  - `grep -rn 'Path("data/' src` returns nothing.
  - Size **M**. Touches `src/models`/`src/preprocessing`, so the
    `ml_training` tier runs.
  *Partial 2026-09-25.* `src/paths.py` (`REPO_ROOT`, `DATA_DIR`,
  `DIDS_YAML`, `BINDINGS_YAML`, `DBC_DIR`, `TEST_PROFILES_DIR`);
  `dashboard/paths.py`, `decode/engineering.py`, `preprocessing/decode.py`,
  `feature_cache.py`, `models/spec_producibility.py`, `decode/dbc.py`,
  `validation/{harness,evidence,synthetic_bus}.py` and the dids/bindings
  defaults in `analysis/{compare,tuning,tuning_frame,lambda_delivery,workbench,
  broadcast_discovery}.py` and `dashboard/routes/rom_maps.py` re-export or use
  it; 16 scripts/tools' `--bindings`/`--dids` defaults too.
  `build_feature_frame_cache_key` takes `dids_path` and hashes it.
  `obd_scheduler._load_did_map` is now public `load_repo_did_map`; the copy in
  `control/obd_request.py` is deleted and `collectors.py` no longer imports a
  private name. `daemon/{runtime,inference}` import `src.paths`. Tests:
  `tests/test_paths.py` (3 pass). The failing-first test
  `test_build_feature_frame_did_columns_do_not_depend_on_the_working_directory`
  is committed as a strict xfail: run with `--runxfail` it fails exactly on the
  bug (`[] == ['did:ECM_RPM', ...]` from a `tmp_path` cwd).
  *Feature-pipeline part done 2026-09-25 (with R04.14):*
  `feature_pipeline.DEFAULT_DIDS_PATH`/`DEFAULT_BINDINGS_PATH` are
  `src.paths.DIDS_YAML`/`BINDINGS_YAML`; `build_feature_frame(dids_path=...)`
  loads that file and threads it into `_feature_frame_cache_metadata`. The
  strict xfail is gone and
  `test_build_feature_frame_did_columns_do_not_depend_on_the_working_directory`
  passes; `test_build_feature_frame_cache_key_hashes_the_dids_file_it_loads`
  shows two catalogs give two cache entries naming their own `dids` path;
  `test_every_default_home_re_exports_the_same_file` now covers
  `feature_pipeline` (`tests/test_paths.py`: 5 pass).
  *Accessors and `channels.py` done 2026-09-25 (remainders pass).*
  `load_repo_did_map`, `_did_ids_by_name`, `known_did_names`, `did_access`,
  `memory_channels` and `did_response_widths` live in `src/decode/dids.py`
  (repo-rooted through `src.paths.DIDS_YAML`, the one `lru_cache`d loader);
  `src.collection.obd_scheduler` re-exports them (its `spec.py` imports them
  for the spec builder), and `control/obd_request.py` and `tools/_bus_tool.py`
  import the new home. `src/analysis/channels.py`'s `read_channels`,
  `read_aligned` and `rescale_channel_rows` default to
  `src.paths.BINDINGS_YAML`/`DIDS_YAML` (six `Path("data/…")` defaults, three
  functions). Tests: `test_hybrid_scheduling.py`, `test_kline_sniff.py`,
  `test_kline_protocol.py`, `test_d02_accounting.py`, `test_did_sweep_tool.py`,
  `test_control_obd_request.py`, `test_analysis_channels.py`,
  `test_paths.py` pass.
  **Remaining:** (4) `grep -rn 'Path("data/' src` still lists `config.py`'s runtime-state
  defaults (`data/control/*`, `data/anomalies`, …) and the module constants
  that mirror them (`reference_epoch`, `vehicle_ledger`, `alerts/tuning`,
  `alerts/feedback`, `training_ledger`, `llm_assist/store`, `mcp_server/config`,
  `custom_pids`, `accepted_baselines`, `feature_pipeline`'s cache dir):
  `profiles.scope_config` rewrites those relative paths per vehicle and tests
  pin them (`test_profiles`, `test_config`), so they wait for R05.1/R05.15's
  profile-relative helpers (R05.15 is not started). `ml_training` not run here.
  *Runtime-state defaults done 2026-09-25 (with R05.15).* `src/paths.py` adds
  `CONTROL_DIR`, `ANOMALIES_DIR`, `ANALYSES_DIR` (repo-rooted, like the
  catalogs); `DaemonConfig`'s `anomalies_dir`, `jobs_dir`, `audit_log`,
  `feedback_log`, `vehicle_ledger`, `reference_epoch`, `training_ledger`,
  `tuning_config`, `tuning_audit`, `trip_state`, `trend_alert_state`,
  `catalog_db`, the new `calibration_dir`, and `LLMAssistConfig.cards_path`
  default through them; the mirrors in `analysis/reference_epoch`,
  `analysis/vehicle_ledger` and `analysis/llm_assist/store` import them. YAML
  values are untouched (a relative `canary.yaml` value still resolves against
  the working directory; `canary.pi.example.yaml`'s `/data/canary/...` pins
  win), and both systemd units `cd "$CANARY_HOME"` (the clone) first, so on the
  Pi the new defaults are the paths the old ones already resolved to.
  `scope_config` scopes absolute defaults the same way.
  `tests/test_paths.py::test_runtime_state_defaults_do_not_depend_on_the_working_directory`
  (every `DaemonConfig` default under `DATA_DIR` is absolute; the three
  analysis mirrors equal their config field), `test_config.py`,
  `test_profiles.py`, `test_repo_tree_isolation.py`,
  `test_daemon.py::test_daemon_config_defaults_are_sane` updated and pass.
  **Remaining:** `grep -rn 'Path("data/' src` still lists (a) the tracked
  per-vehicle catalog defaults in `config.py` (`behavior_models`,
  `accepted_baselines`, `custom_pids`, `capture_profiles`, `afr_calibration`,
  `kline_profile`, `sensor_can.signals`): `tests/test_mcp_behavior_deviations.py`
  `chdir`s into `tmp_path` to redirect the cwd-relative `behavior_models`
  default at a fixture, so repo-rooting these needs that test changed to set
  `daemon.behavior_models` in its YAML (held by the concurrent
  alerts/behavior agent this run); (b) runtime-state mirrors outside this
  dispatch's files, still cwd-relative while their config field is now
  repo-rooted (same file whenever the process starts at the repo root, as the
  units do): `alerts/feedback`, `alerts/tuning` (x2), `models/training_ledger`
  (held by concurrent agents this run), `mcp_server/config` (x2, anomalies and
  catalog fallbacks) and `preprocessing/feature_pipeline`'s cache dir. Each is
  `from src.paths import CONTROL_DIR` (etc.) and one line. The catalog mirrors
  `decode/accepted_baselines` and `collection/custom_pids` move with (a).
  *Remainder done 2026-09-25 (with R06.5); `grep -rn 'Path("data/' src` is
  empty.* (a) `DaemonConfig.behavior_models`, `accepted_baselines`,
  `custom_pids`, `capture_profiles`, `afr_calibration`, `kline_profile` and
  `SensorCanConfig.signals` default to `src.paths.DATA_DIR / …`;
  `tests/test_mcp_behavior_deviations.py` no longer `chdir`s: its YAML sets
  `daemon.behavior_models` (the missing-file test still fails as it should, so
  the fixture file, not the repo's, is the one read). (b) The mirrors import
  `src.paths`: `alerts/feedback.DEFAULT_FEEDBACK_PATH`,
  `alerts/tuning.DEFAULT_TUNING_PATH`/`DEFAULT_TUNING_AUDIT_PATH`,
  `models/training_ledger.DEFAULT_TRAINING_LEDGER_PATH` (`CONTROL_DIR`),
  `mcp_server/config`'s anomalies and catalog fallbacks (`ANOMALIES_DIR`,
  `CONTROL_DIR / "catalog.db"`), `feature_pipeline.DEFAULT_FEATURE_CACHE_DIR`,
  `decode/accepted_baselines.DEFAULT_ACCEPTED_BASELINES_PATH` and
  `collection/custom_pids.DEFAULT_CUSTOM_PIDS_PATH` (`DATA_DIR`).
  Tests: `tests/test_paths.py` 9 pass, including
  `test_mirror_constants_and_per_vehicle_catalogs_are_repo_rooted` (each mirror
  equals its `DaemonConfig` field; each catalog default is absolute under
  `DATA_DIR`), `test_mcp_fallbacks_are_the_daemon_defaults` and
  `test_no_working_directory_relative_data_path_in_src` (the acceptance grep,
  kept as a test); `test_mcp_config.py` asserts the MCP fallback equals
  `DaemonConfig().anomalies_dir`/`catalog_db`; `test_mcp_behavior_deviations.py`,
  `test_config.py`, `test_profiles.py`, `test_capture_profiles.py`,
  `test_custom_pids.py`, `test_accepted_baselines.py`, `test_feedback.py`,
  `test_tuning.py`, `test_training_ledger.py`, `test_sensor_can.py`,
  `test_main.py` pass. The CLI snapshot's `--ledger`/`--feedback`/
  `--feature-cache` defaults now read `<repo>/data/...` (regenerated
  deliberately). `ml_training` run on `test_train_lstm.py`,
  `test_train_tcn.py`, `test_false_positive_benchmark.py`,
  `test_sequence_bakeoff.py`: see R06.5's evidence line.

- [x] **R01.3 One finite-float coercion, in `src/coerce.py`.** The
  "number or `None`" helper has about ten copies with four behaviours:
  - `daemon/inference._finite_or_none` and `_num` (identical bodies, one
    file), `preprocessing/session_context._finite` and
    `decode/behavior_decoder._scalar` refuse NaN and ±inf.
  - `daemon/autotag_hook._num`, `analysis/replay_window._float_or_none` and
    `analysis/events/cold_start._maybe_float` let ±inf through.
  - `preprocessing/live._to_float` ≡ `collection/gps_gpsd._as_float` (AST
    identical) let NaN through and reject `bool`.

  **Do:** add `coerce.finite_float(value) -> float | None`. Migrate the four
  refusing copies and `autotag_hook._num`, whose inf pass-through is a latent
  bug. Leave the NaN-passing pair until each caller has been read, then either
  migrate it or add a named `coerce.float_or_none` with a docstring saying why
  NaN is allowed. **Accept:** one definition per behaviour; `grep -rn "def
  _finite\|def _num\b\|def _to_float\|def _as_float\|def _float_or_none\|def
  _maybe_float" src` is empty or each hit is named here as deliberate. Size
  **S**.
  *Partial 2026-09-25.* `src/coerce.finite_float` (beside the existing
  `optional_str`): `None` for NaN, ±inf, `bool`, and anything `float()`
  refuses. Migrated and deleted: `daemon/inference._finite_or_none` (kept as a
  re-exported alias; `src.daemon` exports it) and `_num`,
  `session_context._finite`, `behavior_decoder._scalar`,
  `autotag_hook._num`, `events/cold_start._maybe_float`,
  `replay_window._float_or_none`, `gps_gpsd._as_float`, `gps_nmea._to_float`,
  `custom_pids._float_or_none`. **Behaviour changes:** `autotag_hook`,
  `cold_start` and `replay_window` now refuse ±inf; `gps_gpsd` refuses NaN;
  NMEA/CSV fields reading `nan`/`inf` parse to `None`; every migrated copy
  except the two that already did (`gps_gpsd`, `gps_nmea` by input type) now
  refuses `bool` where it returned `float(True)` — `daemon/inference`,
  `session_context`, `behavior_decoder`, `autotag_hook`, `events/cold_start`
  and `replay_window` (no caller passes one: all read numeric channels). Tests: `tests/test_coerce.py` (22). Remaining grep hits:
  `cold_start_inventory._finite` is an array filter, not a scalar coercion
  (deliberate); **`preprocessing/live._to_float` remains**: the file is held
  by another session's uncommitted work.
  *Remainder done 2026-09-25.* `live._to_float` is gone; every live coercion
  (`observe` for obd/gps/imu/other, the CAN decode, `_first_finite`) is
  `coerce.finite_float`. Callers read first: the only behaviour that changes
  is a NaN or ±inf sample, which no longer stamps its channel fresh. That is
  the offline rule — the resampler's `_gap_flags` drops NaN before ageing
  (`observed = original.dropna()`), so at HEAD a NaN sample made serve read the
  channel fresh-and-NaN where training held the previous value and aged it.
  No named `float_or_none` was needed. Real-session before/after (R04.4's
  run, 546,953 records of `sessions-0920/2026-09-20T205117`): 351/351 live
  feature rows identical to HEAD's. Test (fails on HEAD's `live.py`):
  `tests/test_preprocessing_live.py::test_a_nan_sample_is_not_an_observation_live_or_offline`.
  `grep -rn "def _finite\|def _num\b\|def _to_float\|def _as_float\|def
  _float_or_none\|def _maybe_float" src` → only
  `cold_start_inventory._finite` (the array filter, deliberate).

- [x] **R01.4 One "numeric column or NaN" helper in
  `src/dataframe_utils.py`.** Six copies:
  - `behavior_input_inventory._values`
  - `cold_start_inventory._column`
  - `session_context._column_values`
  - `behavior_eligibility.floats`
  - `behavior_validation._column`
  - `fingerprint._numeric`, which returns an *empty* series on a missing
    column where the others return all-NaN of frame length

  **Do:** add `numeric_column(frame, name) -> pd.Series` (frame-length, NaN
  when absent). Read each caller of `fingerprint._numeric` before migrating
  it. **Accept:** the six copies are deleted. Size **S**.
  *Done 2026-09-25.* `dataframe_utils.numeric_column` (frame-length, frame
  index, NaN when absent, float dtype) plus `numeric_values` (its ndarray).
  Deleted: `behavior_input_inventory._values`, `cold_start_inventory._column`,
  `session_context._column_values`, `behavior_validation._column`,
  `fingerprint._numeric`; `behavior_eligibility._FrameColumns.floats` is a
  one-line call. `fingerprint._numeric`'s callers were read: every one
  `dropna()`s or takes `first_valid_index()`, except `_add_voltage_stats`,
  whose `voltage.empty` guard became `voltage.isna().all()` (same outcome).
  Tests: `tests/test_dataframe_utils.py` (3), `test_fingerprint.py` green.

- [x] **R01.5 One JSON-sidecar module; one catalog re-index.** Per-session
  sidecar plumbing is copied everywhere:
  - `_update_catalog` has four copies (`analysis/digest.py`,
    `control/dtc_state.py`, `control/classification.py`,
    `control/sessions.py`; three are AST-identical), plus an inline copy in
    `scripts/label_sessions.py`. `trends.py` and `retention.py` have variants.
  - `digest.read_digest`/`read_digest_skipped` are identical to
    `storage/catalog._read_digest_sidecar`/`_read_digest_skipped_sidecar`, and
    catalog already imports from `analysis.digest`. `_read_dtc_sidecar` is
    written twice, and `trip_summary_sidecar.read_trip_summary` is a seventh
    copy.
  - The "if not exists → None; `dict(json.loads(...))`; except
    (OSError, JSONDecodeError)" shape appears 23 times across 16 files. Only
    `status._model_provenance` checks `isinstance(dict)`; the rest raise
    uncaught on valid non-object JSON.
  - The "no `telemetry-*.parquet` → `FileNotFoundError`" preflight appears 3
    times (fingerprint, trip_summary_sidecar, session_classifier).
  - `_dir_size` has four copies. The `companion/library.py` one has no
    `OSError` guard. `status._free_mb` duplicates
    `storage/monitor.free_space_mb`.
  - `_tx` is identical in `companion/library.py` and `storage/catalog.py`, but
    library lacks catalog's WAL + `busy_timeout`.

  **Do:**
  - Create `src/storage/sidecar.py` with `read_json_object(path) -> dict |
    None` (isinstance-checked), `write_json_sidecar` (atomic, sorted keys,
    over `atomic_write_text`) and `require_telemetry(session)`.
  - Add `storage/catalog.record_session_best_effort(db, session)`. Import it
    lazily where needed: catalog imports `analysis.digest`, so a module-level
    import from there would cycle.
  - Put `dir_size` in `src/session.py`.
  - Add a `SqliteStore` base (WAL, `busy_timeout`, `_tx`) for catalog and
    library.

  **Accept:** the copies are deleted. The library gains WAL and a guarded
  `dir_size`, and the evidence names both as behaviour changes. Size **S–M**.
  *Done 2026-09-25.* `src/storage/sidecar.py`: `read_json_object`
  (isinstance-checked), `write_json_sidecar`, `require_telemetry`.
  `catalog.record_session_best_effort` replaces the four `_update_catalog`
  copies and `label_sessions.py`'s inline one (lazy import in `digest`,
  `dtc_state`, `sessions`). Catalog reads the digest via `digest.read_digest*`
  and dtc via `read_json_object`; its three `_read_*_sidecar` copies are gone.
  Migrated readers: digest ×3, trip summary, session metadata, classification,
  fingerprint, reference epoch, yaw offset, capture quality/epoch, trend alert
  state, `status._model_version`/`_model_provenance`, `sequence_detector`,
  `companion/library`, `datasets/{export,health}`, `analysis/tuning`'s
  `capture_profile.json` read (crashed with `AttributeError` on non-object
  JSON) and `gps_tracker._load` (same crash; now starts fresh,
  `test_gps_tracker.py::test_non_object_state_file_starts_fresh`); writers:
  11 sidecar sites.
  Preflight: fingerprint, trip summary, classifier call `require_telemetry`.
  `src.session.dir_size` replaces the four `_dir_size` copies;
  `status._free_mb` calls `storage/monitor.free_space_mb`.
  `storage/sqlite_store.SqliteStore` (WAL, busy timeout, `_tx`) is the base of
  `SessionCatalog` and `CanaryLibrary`. **Behaviour changes:** the library
  gains WAL + `busy_timeout` (so `library.db-wal`/`-shm` appear beside it) and
  an `OSError`-guarded `dir_size`; valid non-object JSON in a sidecar now reads
  as absent instead of raising (`test_storage_catalog.py::test_a_non_object_sidecar_reads_as_absent`,
  failing on HEAD). Left deliberately: JSON reads that report their own errors
  (`role_gate`, `models/manifest`, `training_ledger`, `companion/services`,
  `mcp_server/tools/replay`, `promotion_gate`, `calibration`,
  `behavior_inventory`, `read_capture_manifest`'s pydantic validation), and
  `retention._delete_catalog_rows` / `trends` catalog reads (delete and read,
  not a re-index).

- [x] **R01.6 One `sha256_file`.** `src/models/artifact_hash.sha256_file`
  says in its docstring that it is the only one. There are six more:
  `alert_budget.file_sha256`, `feature_cache._file_sha256`, one in
  `behavior_validation`, `scripts/retrain._file_sha256`,
  `evaluate_detectors.file_sha256` and `benchmark_live_pipeline._sha256`,
  which reads the whole file into memory. **Do:** add
  `sha256_file_or_none(path)` beside it and migrate all six. **Accept:**
  `grep -rn "hashlib.sha256" src scripts tools` shows only `artifact_hash.py`
  and non-file uses. Size **S**.
  *Done 2026-09-25.* `artifact_hash.sha256_file_or_none(path)` (None for
  `None`/non-file/unreadable). `alert_budget.file_sha256`, `retrain._file_sha256`,
  `evaluate_detectors.file_sha256` and `benchmark_live_pipeline._sha256`
  (which read the whole file) are now aliases of the one function;
  `feature_cache._file_sha256` is deleted; `behavior_validation.file_identity`
  / `file_sha256` and `retrain._manifest_fingerprint` call it. Digests are
  byte-identical (feature-cache keys unchanged). The grep shows only
  `artifact_hash.py`, in-memory hashes, and composite multi-file/dataset
  digests (`behavior_validation` dbc dir and session chunks, surface/IF/sequence
  training hashes). Tests: `tests/test_artifact_hash.py` (3).

- [x] **R01.7 One home for operating constants.** Same-meaning constants in
  several places:
  - Engine-running 400 rpm is defined four times: `refit.ENGINE_RUNNING_MIN_RPM`,
    `fingerprint._RUNNING_RPM`, `cold_start_inventory.DEFAULT_MIN_RUNNING_RPM`
    and `dashboard/status._RPM_RUNNING_THRESHOLD`. `models/evaluation.py`
    imports the 2,050-line `refit` only to get it.
  - Highway speed: `events/cruise.py` says ≥70 kph, and the segmenter's
    highway band starts at 80.

  **Do:** create `src/operating.py`, or reuse the vehicle-state module from
  R03.1, holding the engine-running floor, and have the four sites import it.
  Make cruise read the segmenter's band. The vehicle-specific ones (the WOT
  floors in `tuning_digest`, `DEFAULT_RPM_BANDS` ending at the VQ40 redline)
  belong to R05.13, not here. **Accept:** one definition of each. Size **S**.
  *Partial 2026-09-25.* `src/operating.py`: `ENGINE_RUNNING_MIN_RPM` and
  `HIGHWAY_MODE`. `fingerprint._RUNNING_RPM`,
  `cold_start_inventory.DEFAULT_MIN_RUNNING_RPM` and
  `dashboard/status._RPM_RUNNING_THRESHOLD` alias it; `models/evaluation`,
  `models/synthetic_fault_eval` and `behavior_input_inventory` import it
  (neither `models` module imports `analysis.refit` any more). Cruise reads the
  segmenter's label: `regime` is `highway` when most of the window is
  segmenter-`highway`. **Behaviour change:** a 70–80 km/h cruise the segmenter
  calls `city` is now `surface` (was `highway`);
  `test_events.py::test_cruise_regime_is_the_segmenters_band_not_its_own`,
  and the existing highway test now labels its rows `highway`.
  `CLASSIFIER_VERSION` 0.3.0 → 0.3.1 so stale classification sidecars
  recompute with the new regime.
  *Remainder done 2026-09-25.* `src/analysis/refit.py` now does `from
  src.operating import ENGINE_RUNNING_MIN_RPM` (still in `refit.__all__`; its
  KOEO rationale comment kept beside the import). The verifier found two
  more literal copies by value, not name: `decode/behavior_models`
  `EngineStateRequirement.min_running_rpm` and
  `ColdStartRequirement.min_running_rpm` defaulted to `400.0`; both now
  default to `ENGINE_RUNNING_MIN_RPM` (same float, so the recipe golden
  fingerprints are unchanged). Acceptance grep widened to numeric literals:
  `grep -rnE "\b400(\.0)?\b" src --include=*.py` minus HTTP status codes
  and prose leaves one rpm hit, `src/operating.py:19`; the name grep finds
  only aliases of it. `test_refit.py`,
  `test_recipe_catalog_golden.py`, `test_layering.py` pass.

- [x] **R01.8 Fix the layering inversions.** Imports run both ways between
  analysis and everything else:
  - `decode` imports `analysis`: `behavior_decoder` imports
    `surface_1d.evaluate_1d` / `surface_2d.evaluate_2d` (moved by R05.10).
  - `models` imports `analysis`: `models/evaluation.py` imports `refit` for
    one constant (R01.7).
  - `daemon` and `control` import `dashboard`: `daemon/inference.py` and
    `control/nonvolatile_write.py` import `src.dashboard.status` (moved by
    R03.1).
  - `daemon/runtime` and `daemon/inference` import
    `dashboard/paths.DEFAULT_BINDINGS_PATH` (moved by R01.2).
  - `control/session_timeseries.py` and `control/binned_analysis.py` import
    `mcp_server/parquet_query.TelemetryQuery`. Move it to
    `storage/telemetry_query.py` and leave a shim.
  - `alerts/emitter.IsolationForestInferenceEngine` belongs in
    `models/inference.py` beside `SequenceInferenceEngine`.

  **Do:** the moves above. This item closes when the named inversions are
  gone. **Accept:** a small `tests/test_layering.py` that walks `src/` with
  `ast` and fails on the named edges: `decode→analysis`,
  `{daemon,control,models}→dashboard`, `control→mcp_server` and
  `models→analysis.refit`. `models`/`preprocessing` importing
  `analysis.drives`/`derived`/`pairing` is legitimate today (those are the
  single sources), so the test forbids edges and allows the rest, rather than
  imposing a strict layer order. That keeps the inversions from coming back.
  Size **S** after R01.2, R03.1 and R05.10.
  *Partial 2026-09-25.* `TelemetryQuery` moved to
  `src/storage/telemetry_query.py`; `mcp_server/parquet_query.py` is a
  re-export shim; `control/{session_timeseries,binned_analysis}` and the MCP
  server/tools import the new home. `IsolationForestInferenceEngine` moved to
  `models/inference.py`; `alerts/emitter` re-exports it lazily (module
  `__getattr__`), `daemon/inference` imports the new home.
  `daemon/{runtime,inference}` take `DEFAULT_BINDINGS_PATH` from `src.paths`;
  `models/{evaluation,synthetic_fault_eval}` take the rpm floor from
  `src.operating`. `tests/test_layering.py` forbids `decode→analysis`,
  `{daemon,control,models}→dashboard`, `control→mcp_server` and
  `models→analysis.refit`; on a HEAD worktree it fails naming the six R01
  edges. Two allowance tables are stale-checked (an allowance whose edge is
  gone fails the test): **`OWED`**: `decode/behavior_decoder →
  analysis.surface_1d/surface_2d` (R05.10) and `daemon/inference`,
  `control/nonvolatile_write → dashboard.status` (R03.1). **`STANDING`**
  (daemon→dashboard edges R01.8 does not name, recorded so a new one fails):
  `daemon/server → dashboard.api` (composition root), `daemon/{components,
  drain,inference} → dashboard.broadcast`, `daemon/{components,inference} →
  dashboard.behavior_live`, `daemon/idle_shutdown → dashboard.ups`.
  **Remaining:** the `OWED` edges, closed by R03.1 and R05.10.
  *Closed 2026-09-25.* R03.1 and R05.10 landed: `tests/test_layering.py`'s
  `OWED` is `{}` (the stale-allowance check forced the entries out), and
  `grep` finds no `decode→analysis.surface_*`, no
  `{daemon,control,models}→dashboard.status`, no `control→mcp_server`, no
  `models→analysis.refit` import in `src/` (the only `dashboard.status` hit
  is prose in `daemon/inference_metrics.py`'s docstring). The remaining
  daemon→dashboard edges are the recorded `STANDING` ones. All five
  `test_layering.py` tests pass (with this dispatch's `refit` import change).

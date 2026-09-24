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

- [ ] **R01.1 One session-name module: kind, start time, ordering.**
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

- [ ] **R01.2 One repo-rooted home for default data paths.**
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

- [ ] **R01.3 One finite-float coercion, in `src/coerce.py`.** The
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

- [ ] **R01.4 One "numeric column or NaN" helper in
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

- [ ] **R01.5 One JSON-sidecar module; one catalog re-index.** Per-session
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

- [ ] **R01.6 One `sha256_file`.** `src/models/artifact_hash.sha256_file`
  says in its docstring that it is the only one. There are six more:
  `alert_budget.file_sha256`, `feature_cache._file_sha256`, one in
  `behavior_validation`, `scripts/retrain._file_sha256`,
  `evaluate_detectors.file_sha256` and `benchmark_live_pipeline._sha256`,
  which reads the whole file into memory. **Do:** add
  `sha256_file_or_none(path)` beside it and migrate all six. **Accept:**
  `grep -rn "hashlib.sha256" src scripts tools` shows only `artifact_hash.py`
  and non-file uses. Size **S**.

- [ ] **R01.7 One home for operating constants.** Same-meaning constants in
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

- [ ] **R01.8 Fix the layering inversions.** Imports run both ways between
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

# R09 — package layering: one direction for imports

Opened 2026-09-27 from a whole-repo complexity review. Same conventions as
the other task files: `[x]` only with evidence, `[~]` partial with the rest
named. R00's shared acceptance rules apply to every item here
([R00](R00-refactor-README.md#shared-acceptance-rules)).

Imports between `src/` packages run both ways. There are 91
package-to-package edges, and 19 pairs of packages import each other. In 16
of those pairs both directions are runtime imports, some deferred inside
functions; the other 3 close only through `TYPE_CHECKING` imports. The count
is rising:

| date | package edges | pairs importing each other |
|---|---|---|
| 2026-05-20 | 31 | 3 |
| 2026-08-15 | 62 | 12 |
| 2026-09-15 | 71 | 13 |
| 2026-09-27 | 91 | 19 |

The cost shows up in three ways:

- **Fixes that miss callers.** AGENTS.md's "a fix every caller must remember"
  bugs come from shared functions whose callers sit in every layer.
- **Hidden cycles.** There are 148 function-level cross-package imports, and
  some exist only to dodge a cycle (`models ↔ plausibility`, `alerts →
  models`).
- **New edges in the foundation.** `config.py`, the schema every package
  imports, took on an import of `storage` in `249fccd`.

The 19 pairs come from four misplacements, not nineteen:

1. **`analysis/` holds runtime building blocks.** The runtime imports 37 of
   its modules. Calls from `control`, `dashboard` and `mcp_server` into the
   workbench point the right way. The wrong direction is a dozen modules that
   lower layers need:
   - channel physics: `derived`, `committees`, `afr`, `fsm`, `pairing`;
   - `drives`;
   - the vehicle's own records: `tune_revisions`, `vehicle_ledger`;
   - the digest, fingerprint and classification sidecar readers.
2. **`control/` holds session-storage basics.** `control.sessions` (AGENTS.md's
   "one parser") and `control.capture_manifest` are imported by `analysis`,
   `models`, `datasets` and `storage`.
3. **`preprocessing/` mixes a base layer with an assembly layer.**
   `feature_pipeline.py`, `live.py` and `feature_cache.py` combine the base
   (loader, resampler, features, segmenter) with physics channels, committees
   and behaviour deviations. `decode/behavior_*` goes back the other way: it
   is model code built on `preprocessing` features.
4. **Column-name constants live in `preprocessing`.** 58 of the 70
   cross-package imports from `resampler`, `segmenter` and `features` take
   only naming constants such as `TIMESTAMP_COLUMN`, `MODE_COLUMN` and
   `gap_column`.

R01.8 added `tests/test_layering.py`, which forbids six named edges and keeps
stale-checked `OWED` and `STANDING` allowance tables. R09 keeps that
mechanism and turns the named edges into a layer order.

## Target layers

```
9  entry        main, validation
8  interface    dashboard, companion, mcp_server
7  runtime      daemon, startup
6  application  control, vehicle_state
5  workbench    analysis
4  detection    models, alerts, plausibility, datasets,
                behavior (new, from decode/behavior_*),
                features (new, from preprocessing/{feature_pipeline,live,feature_cache})
3  pipeline     preprocessing (loader, resampler, features, segmenter, drives, …)
2  acquisition  collection
1  persistence  storage (+ sessions, capture manifests, session-product readers),
                vehicle (new: profiles, tune_revisions, vehicle_ledger)
0  foundation   decode (byte → engineering unit),
                signals (new: derived, committees, afr, vehicle_signals, fsm, pairing),
                atomic_write, artifact_hash, can_ids, coerce, columns (new), config,
                dataframe_utils, jsonl_store, log, operating, paths, session,
                telemetry, timefmt, trainability
```

The rules the test enforces:

- **Direction.** A module imports only from its own layer or lower.
- **No pairs.** Two packages never import each other, even within one layer.
- **Every import counts.** Function-level and `TYPE_CHECKING` imports count,
  as they already do in the R01.8 test.
- **One standing exception.** `daemon/server.py → dashboard.api` is the
  composition root: the daemon serves the app.

`dashboard` sits above `daemon`, as R01.8's forbidden `daemon → dashboard`
edge already implies. The daemon-owned objects that live in `dashboard/`
today move down (R09.8).

## Rules

- **The layering test is the ledger.** R09.1 records every current violation
  in `OWED`, with the item that removes it. An item closes when its entries
  are gone, and the stale check forces them out of the table.
- **Pure moves.** No item changes behaviour. A move that turns out to need a
  logic change stops, and the change becomes an item of its own.
- **Shims are temporary.** Under R00's rule an old path may re-export while
  callers remain. An R09 item closes only when:
  - every caller has moved, including `mock.patch("src.…")` strings, which
    mypy cannot see;
  - the shim is deleted;
  - `grep -rn` over `src tests scripts tools` finds no import of the old path.

  A shim left behind keeps a second import path writable.
- **New packages that feed `models/` join `scripts/ml_paths.txt`** in the
  item that creates them: `src/signals/`, `src/behavior/`, `src/features/` and
  `src/columns.py`. Otherwise a change there silently stops selecting the
  `ml_training` tier.
- **Model binaries are unaffected (checked 2026-09-27).** The IF payload
  (`joblib`) and the sequence payload (`torch.save`) store the scaler and the
  manifest as `to_dict()` output, not `src` classes. `artifact_fingerprint`
  hashes the fitted surface (name, kind, output, fit), not module paths. If a
  move changes a fingerprint anyway, stop: deployed quarantines are pinned to
  those values.
- **Files dirty from another session are not moved until they are clean.**
  Run `git status` before starting an item. On the morning of 2026-09-27
  `preprocessing/feature_pipeline.py` and `models/evaluation_detectors.py`
  were dirty; `34f9886` committed them.

## Operator decision (do not re-open)

- **R09.1: replace R01.8's named-edge rule with a strict layer order?**
  **Decided 2026-09-27 (operator): yes.** `tests/test_layering.py` enforces
  the order above.
  R01.8 deliberately forbade named edges "rather than imposing a strict layer
  order". It allowed `models` and `preprocessing` to import
  `analysis.drives`, `derived` and `pairing` because those modules are the
  single sources. R09 keeps them single sources but moves them beneath their
  importers (R09.4). **Recommendation:** adopt the order. The named-edge list
  stopped the six inversions it named, but in the same period the pair count
  rose from 13 to 19.

## Starting ledger (2026-09-27)

Under the map above, the tree has 85 upward file → module edges and 3
same-layer pairs. One edge is the standing composition root, so R09.1 starts
with 84 `OWED` edges plus the 3 pairs.

| from → to (modules imported) | item |
|---|---|
| `analysis` → `control.sessions`, `control.capture_manifest` | R09.3 |
| `analysis` → `control.dtc_state`, `control.trip_end` | R09.2 |
| `preprocessing` → `analysis.{derived,committees,afr,fsm,pairing,drives}` | R09.4 |
| `daemon` → `dashboard.{broadcast,behavior_live,ups}` | R09.8 |
| `daemon/server.py` → `dashboard.api` | `STANDING` |
| `decode` → `preprocessing.{features,local_range,session_context}` (R09.2 cleared `resampler`, `segmenter`) | R09.6 |
| `models` → `analysis.drives` | R09.4 |
| `models` → `analysis.session_classifier` | R09.3 |
| `models` → `analysis.tune_revisions` | R09.5 |
| `models` → `analysis.training_disposition` | R09.2 |
| `models` → `control.{sessions,capture_manifest}` | R09.3 |
| `profiles` → `analysis.{tune_revisions,vehicle_ledger}`, `collection.*` | R09.5 |
| `collection` → `analysis.derived` | R09.4 |
| `collection` → `analysis.tune_revisions`, `control.calibration` | R09.5 |
| `collection` → `preprocessing.loader` | R09.3 |
| `storage` → `collection.{capture_profiles,record_arming}` | R09.2 |
| `storage` → `analysis.{digest,fingerprint,session_classifier}` | R09.3 |
| `storage` → `preprocessing.{loader,decode}` | R09.3 |
| `storage` → `control.dtc_state` | R09.2 |
| `alerts` → `analysis.committees` (`TYPE_CHECKING`) | R09.4 |
| `control` → `daemon.{health,components}` (`TYPE_CHECKING`) | R09.8 |
| `datasets` → `control.sessions` | R09.3 |
| `trainability` → `analysis.{committees,derived}` | R09.4 |
| `trainability` → `preprocessing.features` | R09.2 |
| `config` → `storage.broadcast_storage`, `collection.lsm6dsox` | R09.2 |
| `plausibility` → `analysis.fsm` | R09.4 |
| `preprocessing` → `models.artifact_hash` | R09.2 |
| pair `models ↔ plausibility` | R09.2 |
| pair `alerts ↔ models` | R09.7 |
| pair `companion ↔ dashboard` | R09.8 |

## Order

1. **R09.1 first, now.** It needs nothing else and stops the growth.
2. **R09.2 → R09.3 → R09.4 → R09.5.** R09.4 needs `columns.py` (R09.2) and
   `storage.sessions` (R09.3); R09.5 needs `storage.sessions` too.
3. **R09.6 after R09.4,** checking first that no other session has
   `feature_pipeline.py` open.
4. **R09.7 and R09.8 any time after R09.1.**
5. **R09.9 last.**

**Tier cost:** R09.2, R09.3, R09.4, R09.6 and R09.7 touch `src/models/` or
`src/preprocessing/`, so their pushes run `ml_training`. Batch them.

No task here changes the order in `OVERNIGHT-QUEUE.md`.

## Relation to existing checklists

- **R01.8:** R09.1 extends its test. `test_the_edges_r01_removed_stay_removed`
  stays as an explicit pin. R01.8 recorded the `daemon → dashboard` edges in
  `STANDING`, noting that each "wants a home decision of its own"; R09.8
  makes those decisions.
- **H01 (source policy):** R00 requires that the layering test not forbid
  imports H01 relies on. Those imports are either legal under the map or owed
  to R09.4, and they become legal when R09.4 lands. For example,
  `trainability → analysis.derived` becomes `trainability → signals.derived`,
  within layer 0.
- **P02 / H02 / H07 (in flight):** `storage/broadcast_storage.py` landed in
  `249fccd` and is imported downward everywhere except by `config.py`, which
  R09.2 fixes.
- **R05.16 (open):** it works in `analysis/lambda_delivery*`, which R09 does
  not move.

---

- [x] **R09.1 Turn `tests/test_layering.py` into a layer order.**
  **Do:**
  - Add a `LAYERS` table: top-level packages and root modules (`src/*.py`,
    keyed one by one) mapped to the layers above. Keep it in the test beside
    `OWED`/`STANDING`, as the file does today.
  - Two checks, both with function-level and `TYPE_CHECKING` imports
    included:
    - an import into a higher layer fails;
    - two packages importing each other fails, even within one layer.
  - A package or root module missing from `LAYERS` fails, so a new one must
    be placed.
  - Record the starting ledger in `OWED`, each value naming its R09 item. The
    eight non-`server.py` entries move from `STANDING` into `OWED` (R09.8), so
    `STANDING` keeps only `daemon/server.py → dashboard.api`.
  - Drop the `FORBIDDEN` tuple, which the order covers. Keep
    `test_the_edges_r01_removed_stay_removed`, `test_src_never_imports_tests`
    and `test_validation_harness_imports_without_pytest`.

  **Accept:**
  - The test passes on the tree with `OWED` populated.
  - A scratch `from src.analysis.derived import …` added to a `storage`
    module fails and names the file.
  - Deleting an `OWED` entry whose edge still exists fails; deleting an edge
    whose entry remains fails the stale check.
  - The test parses only (no imports) and stays under a second.

  Size **S**.
  *Done 2026-09-27.* `tests/test_layering.py`:
  - **Tables.** `LAYERS` places 32 units (15 packages, 17 root modules). `OWED` holds
    84 upward imports, and every value is an item ID (R09.2 ×11, R09.3 ×24,
    R09.4 ×21, R09.5 ×9, R09.6 ×9, R09.8 ×10), generated by the test's own
    scanner so the two cannot disagree. `OWED_PAIRS` holds the three pairs:
    `models ↔ plausibility` (R09.2), `alerts ↔ models` (R09.7) and
    `companion ↔ dashboard` (R09.8). `STANDING` is `daemon/server.py →
    dashboard.api` alone. The eight other `daemon → dashboard` entries it
    used to hold are now in `OWED` under R09.8.
  - **R01.8 pins.** `FORBIDDEN` is gone. `test_the_edges_r01_removed_stay_removed`
    now checks every import, not only violations, so R01.8's edges
    (`decode → analysis`, `control → mcp_server`, `models → analysis.refit`,
    `dashboard.paths`, `{control,models} → dashboard`) cannot come back even
    as an `OWED` entry.
  - **Scanner tests.** Two tests run the scanner on synthetic trees: a
    function-level upward import is flagged, a downward one is not, and a
    same-layer pair that closes through `TYPE_CHECKING` is flagged.
  - **Negative checks**, against a scratch copy of `src/`:
    - a lazy `from src.analysis.derived import …` appended to
      `storage/sidecar.py` fails, naming `storage/sidecar.py ->
      src.analysis.derived`;
    - popping a live `OWED` entry fails the upward check;
    - a bogus `OWED` or `OWED_PAIRS` entry fails the stale check;
    - dropping `datasets` from `LAYERS` fails with "place these in LAYERS".
  - **Speed.** A full `ast.walk` made the scan take 2.1 s, which would have
    put the file in the `slow` tier and out of the push gate. The walker now
    descends only through statement bodies (imports are statements). It
    finds exactly the same imports as `ast.walk` in every `.py` file under
    `src tests scripts tools`, and it parses each file once for all the
    checks. The scan takes 0.70 s; the slowest test in the file is 0.98 s
    (the existing pytest-free harness probe).
  - **Result.** 9 passed; ruff and mypy are clean.

- [x] **R09.2 Leaf moves: constants and single-purpose helpers.**
  Each move takes a name out of a higher layer into the lowest layer that
  owns it:
  - **`src/columns.py`**, holding the frame-column conventions now in
    `preprocessing/{resampler,segmenter,features}`:
    - names: `SESSION_COLUMN`, `TIMESTAMP_COLUMN`, `GAP_SUFFIX`,
      `ARRIVED_SUFFIX`, `MODE_COLUMN`, `UNKNOWN_MODE`, `ROC_SUFFIX`;
    - helpers: `gap_column`, `arrived_column`, `is_flag_column`,
      `roc_column`, `base_channel`.

    This covers the 58 imports that take only naming constants and clears
    `trainability → preprocessing.features`.
  - **`models/artifact_hash.py` → `src/artifact_hash.py`.** Clears
    `preprocessing → models`.
  - **`control/dtc_state.SIDECAR_FILENAME` (`"dtc.json"`) → `src/session.py`**,
    which already holds the session filesystem conventions. Clears
    `storage → control` and one `analysis → control` edge.
  - **The digest's budgets.** `analysis/digest.py` lazily imports
    `digest_row_budget` / `digest_span_budget_seconds` from
    `control/trip_end.py` (two sites). Pass them in through its existing
    `DigestBudgets` type from the callers instead. If an `analysis` caller
    needs the derived default, move the two functions and the child-ceiling
    constants they read into `analysis/digest.py`, and let `control` import
    them from there. Record which route you took.
  - **`inject_realistic_fault`** and its channel constants move from
    `models/synthetic_fault_eval.py` to `preprocessing/anomaly_injection.py`,
    the existing injection home. This clears
    `plausibility → models` and so the `models ↔ plausibility` pair.
  - **`analysis/training_disposition.GoldenBaselines` → `models/`.** Its only
    outside user is `models/golden_baselines.py`.
  - **The storage writer stops importing `collection`.**
    `parquet_writer.py` and `async_writer.py` type against two small
    Protocols declared in `storage/`:
    - a profile Protocol exposing `name`, `storage_prefix`,
      `store_every_frame` and the pydantic dump methods they call;
    - an arming-gate Protocol exposing `observe` and
      `reset_for_new_session`.
  - **`config.py` stops importing `storage` and `collection`.**
    - `DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS` and
      `validate_broadcast_stale_timeout` (added in `249fccd`) move into
      `config.py`, and `broadcast_storage` imports them from there.
    - The LSM6DSOX validator (`config.py` ~l.252) builds the driver's
      `ImuSettings` to check rates and ranges. Move the allowed-value tables
      it needs somewhere with no `collection` import, and have the driver
      import them.

  **Accept:**
  - Those `OWED` entries are gone.
  - `models ↔ plausibility` no longer appears.
  - Serve path (the storage writer and `config`): a pinned-session replay
    before and after gives an empty event and row diff.

  Check that `models/evaluation_detectors.py` is clean first (it lazily
  imports `plausibility`). Size **M**.
  *Done 2026-09-27.* Every move is a pure move: the definitions were moved,
  not copied, and an AST comparison of each moved definition between
  `2ec9383` and the working tree finds them identical. The two exceptions are
  a reworded `ChannelBaseline` docstring and `np.isfinite` → `math.isfinite`
  on a Python `float`. No old module re-exports a moved name, and an AST
  sweep of `src tests scripts tools` finds no import of one from its old home.
  - **`src/columns.py`** (new, layer 0) holds `SESSION_COLUMN`,
    `TIMESTAMP_COLUMN`, `GAP_SUFFIX`, `ARRIVED_SUFFIX`, `FLAG_SUFFIXES`,
    `MODE_COLUMN`, `UNKNOWN_MODE`, `MEAN_SUFFIX`, `STD_SUFFIX`, `ROC_SUFFIX`,
    `ROLLING_SUFFIXES`, `gap_column`, `arrived_column`, `is_flag_column`,
    `split_rolling`, `base_channel`, `mean_column`, `std_column` and
    `roc_column`. They are all pure string helpers, and none was left behind.
    - 102 files import them from `src.columns`. The three preprocessing
      modules import only the names they use themselves.
    - `features` used to re-export `SESSION_COLUMN` and `TIMESTAMP_COLUMN`,
      and `src/preprocessing/__init__`'s lazy `_EXPORTS` served `MODE_COLUMN`,
      `UNKNOWN_MODE`, `gap_column`, `mean_column`, `std_column` and
      `roc_column`. Both routes are gone.
    - `CONTEXT_COLUMN_PREFIX` stays in `features`, because only
      `preprocessing` imports it.
    - `trainability` now imports `base_channel` at top level (the cycle it
      dodged is gone).
    - `analysis.derived` no longer imports `preprocessing` at all.
  - **`src/artifact_hash.py`** (layer 0) is the old
    `models/artifact_hash.py`. Its 12 importers were retargeted.
  - **`dtc.json`** moved to `src/session.py` as `DTC_SIDECAR_FILENAME`.
    `session.py`'s other names are `FRAMES_FILENAME` etc., and a bare
    `SIDECAR_FILENAME` would be ambiguous there. `dtc_state`,
    `analysis/digest`, `storage/catalog` and two tests import it from
    `src.session`.
  - **Digest budgets: second route.** `analysis.digest.get_or_compute_digest`
    calls `digest_preflight(session_dir, config)`, and its HTTP/MCP callers
    pass no budgets, so an `analysis` caller needs the derived default. The
    following moved into `analysis/digest.py`:
    - `digest_row_budget` and `digest_span_budget_seconds`;
    - the constants they read: `DIGEST_PEAK_BYTES_PER_ROW`,
      `DIGEST_CHAIN_FIXED_BYTES` and `DIGEST_CHAIN_BYTES_PER_SECOND`;
    - `digest_address_space_bytes`, `_physical_memory_bytes`,
      `_ADDRESS_SPACE_*` and `_MIB`. The derived default goes through these.

    `control/trip_end.py` imports `digest_address_space_bytes` and `_MIB`
    from there. `control/workbench_jobs.py` and `tests/test_trip_end_digest.py`
    import the budgets from `analysis.digest`.
  - **Realistic faults.** The whole A02.6 section of `synthetic_fault_eval`
    moved to `preprocessing/anomaly_injection.py`:
    - `inject_realistic_fault` and `choose_fault_window`;
    - `load_fault_channel_roles`, `REALISTIC_FAULTS`, `REALISTIC_FAULT_ROLES`,
      `RealisticFault`, `RealisticInjection` and `RealisticFaultKind`;
    - the `*_CHANNEL` constants, the thermostat/trim/leak constants, and the
      private masks and appliers.

    `choose_fault_window` shares the private masks, so leaving it in `models`
    would have meant importing private helpers across layers.
    `plausibility/offline.py`, `scripts/evaluate_{detectors,plausibility}.py`
    and two test files import them from there. `grep src.models src/plausibility`
    is empty, lazy imports included.
  - **`ChannelBaseline` and `GoldenBaselines`** moved into
    `models/golden_baselines.py`. `classify_dispositions`,
    `compute_quarantine_mask` and `compute_golden_baselines` stay in
    `analysis/training_disposition.py` and import them downward.
  - **Storage Protocols** in the new `storage/protocols.py`:
    - `StorageProfile` exposes the `name`, `storage_prefix` and
      `store_every_frame` properties plus `model_dump(*, mode)`;
    - `RecordGate` exposes `observe(record) -> bool` and
      `reset_for_new_session()`.

    `parquet_writer` and `async_writer` are typed against them, and mypy
    checks `CaptureProfile` and `RecordArmingGate` structurally at
    `control/profile_capture.py`, `validation/harness.py` and
    `daemon/runtime.py`.
  - **`config.py`** now owns `DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS`,
    `MIN_BROADCAST_STALE_TIMEOUT_SECONDS`, `REWRITE_LEAD_SECONDS` (the
    validator reads it) and `validate_broadcast_stale_timeout`.
    `broadcast_storage`, `preprocessing/resampler`, `plausibility/limits` and
    `storage/parquet_writer` import them from config.
  - **LSM6DSOX tables.** `ODR_CODES`, `ACCEL_RANGES`, `GYRO_RANGES` and
    `ImuSettings` moved to the new root module `src/lsm6dsox_settings.py`
    (layer 0, no imports), so the tables and the check stay one definition.
    - `config.ImuConfig` imports `ImuSettings` at top level. The driver,
      `imu_collector` and `test_lsm6dsox` import it from the new module.
    - The error messages are unchanged:
      `test_config_refuses_a_rate_the_lsm6dsox_cannot_do` passes.
    - `test_process_state_reset.NOT_STATE` lost its three `lsm6dsox.py`
      entries, because the tables left `collection/`.
  - **Ledger.**
    - `LAYERS` gains `columns`, `artifact_hash` and `lsm6dsox_settings` (all 0).
    - `OWED` went from 84 to **69**: the 11 R09.2 entries, plus 4 R09.6
      entries that the columns move also cleared.
      `decode/behavior_decoder.py → preprocessing.resampler` and
      `decode/behavior_eligibility.py → preprocessing.{features,resampler,segmenter}`
      imported only column names, and the stale check forced them out.
    - `OWED_PAIRS` lost `models ↔ plausibility`.
    - No new upward edge or pair appeared.
  - **`scripts/ml_paths.txt`** adds `src/columns.py` and `src/artifact_hash.py`.
    The second keeps the tier coverage it had under `src/models/`.
  - **Patch targets.** None needed retargeting: no `mock.patch` or
    `monkeypatch.setattr` string or module-attribute patch names a moved
    symbol. Two tests changed only their import lines:
    `test_cold_start_imports.test_preprocessing_package_lazy_reexports_still_resolve`
    now probes `ResampleConfig`, and `test_train_anomaly_model` imports
    `MODE_COLUMN` from `src.columns`.
  - **Gate.**
    - `ruff check .` and `ruff format --check .` are clean, and `mypy` finds
      no issues in 428 files.
    - `tests/test_layering.py`: 9 passed.
    - Inner-loop tier (`-m "not integration and not slow"`): 6393 passed,
      1 skipped.
    - Slow tests in the 33 affected files: 87 passed, 1 xfailed.
    - `-m integration tests/test_replay_isolation.py`: 1 passed.
    - `-m ml_training`: 122 passed.
  - **Replay.** `tools/replay_event_diff.py ab`, `2ec9383` vs the working
    tree, session `2026-09-18T213818`, 900 s, deployed IF
    `2026-09-20T054703` + LSTM `2026-09-20T064628`:
    - 901 ticks and 18 events on both sides;
    - events identical: 18 of 18 paired, 8316 floats, max |delta| 0;
    - scores identical: IF 0 of 901, LSTM 0 of 872 and fused 0 of 872 differ,
      5255 floats, max |delta| 0;
    - counters and meta identical.

    The tool diffs events and scores, not stored rows. In place of the row
    diff R00 asks for, the verifier showed that `ParquetTelemetryWriter` and
    `AsyncParquetTelemetryWriter` are AST-identical to `2ec9383` with
    annotations stripped, and that nothing they call changed apart from the
    `math.isfinite` line. So the stored rows cannot differ.
    **Verified 2026-09-27 (verifier): earned, with nits.** All 65 moved
    definitions are AST-identical to their originals, apart from the three
    declared exceptions. Every one of the 3,209 `from src.* import` pairs in
    `src tests scripts tools` resolves.

- [x] **R09.3 `storage` owns the session.**
  - **`control/sessions.py` → `storage/sessions.py`, whole.** After the moves
    below, everything it imports is at layer 1 or below.
  - **`control/capture_manifest.py` → `storage/capture_manifest.py`.**
    `normalize_quality_payload`, `SCHEMA_VERSION` and the `SCOPE_*`
    constants from `collection/capture_quality.py` move with it: they are the
    stored payload's schema migration. `capture_quality` imports them back,
    which is downward.
  - **File-level session helpers.** `PARQUET_PATTERN`, `has_telemetry`,
    `count_session_rows` and `session_time_span_seconds` move from
    `preprocessing/loader.py` into `storage/`.
  - **Session-product readers:** `read_digest`, `read_digest_skipped`,
    `read_fingerprint`, `read_classification` and their filename constants
    move to `storage/session_products.py`. Computing and writing stay in
    `analysis`. If a reader returns an `analysis` dataclass, `storage` gets
    the plain-dict reader and `analysis` wraps it.
  - **Catalog functions.** `can_channel_spellings` and `on_read_catalog`
    move from `preprocessing/decode.py` into `src/decode/`; they need only
    `decode.*`, `can_ids`, `telemetry` and `paths`. `TelemetryQuery` stays
    in `storage/`, the home R01.8 chose. The session-reading function in
    `preprocessing/decode.py` stays.
  - **AGENTS.md.** Update the paragraphs that name
    `src.control.sessions.session_started_at` / `latest_session`.

  **Accept:**
  - Every `OWED` entry in the ledger rows for R09.3 is gone.
  - `grep -rnE "src[./]control[./](sessions|capture_manifest)" src tests
    scripts tools` finds nothing (`control.sessions` was referenced from 57
    files).
  - A pinned-session replay gives an empty diff.

  Size **M**.
  *Done 2026-09-27.* Every move is a pure move. An AST comparison of the 85
  moved definitions between `5f6717b` and the working tree finds them
  identical, apart from the renames below and the two lazy
  `session_started_at` imports inside `capture_manifest`, which now name
  `src.storage.sessions`. No old module defines or re-exports a moved name
  (an old home that uses one imports it for itself and leaves it out of
  `__all__`). Re-running the import rewriter over `src tests scripts tools`
  finds no import of a moved name from its old home.
  - **Whole modules** (`git mv`): `control/sessions.py` →
    `storage/sessions.py` and `control/capture_manifest.py` →
    `storage/capture_manifest.py`. `storage/sessions.py` now imports only
    `session`, `storage.capture_manifest`, `storage.session_files`,
    `storage.sidecar` and, lazily, `storage.catalog`.
  - **`collection/capture_losses.py` → `storage/capture_losses.py`, whole**
    (`git mv`). This file is not named in the item.
    `normalize_quality_payload` calls `capture_losses.unknown_losses()`, and
    that calls `summarize_losses`, so the payload migration still pointed up
    into `collection`. The module imports nothing, so it moved whole rather
    than being split. Its six importers were retargeted: `capture_quality`,
    `control/capture_finalize`, `daemon/health`, `validation/evidence` and
    three tests. `test_process_state_reset.NOT_STATE` lost its
    `capture_losses.py` entry, because the table left `collection/`.
  - **The quality payload's schema** moved into `storage/capture_manifest.py`:
    `normalize_quality_payload`, the four `SCOPE_*` constants, and
    `SCHEMA_VERSION`, renamed `CAPTURE_QUALITY_SCHEMA_VERSION`. The same
    module already has `CaptureManifest.schema_version` (1), so a bare
    `SCHEMA_VERSION = 2` there would be ambiguous. This follows R09.2's
    `DTC_SIDECAR_FILENAME` rename. `capture_quality` imports back the two it
    uses (`CAPTURE_QUALITY_SCHEMA_VERSION`, `SCOPE_LIVE`).
    `analysis/capture_coverage`, `control/capture_finalize` and
    `test_capture_quality` import them from storage.
  - **File helpers: new `storage/session_files.py`.** It holds
    `PARQUET_PATTERN`, `has_telemetry`, `count_session_rows` and
    `session_time_span_seconds`. Those two counters are thin wrappers over
    the footer reader, so `SessionFooterSpan`, `session_footer_span` and
    `_footer_utc` moved too. `_footer_utc` is renamed `footer_utc`, because
    the loader's `_chunk_time_span` still calls it and a private name should
    not cross modules.
    - Why a separate module rather than `storage/sessions.py`: `sessions` is
      directory-level (names, ordering, pins, export) and needs only the
      stdlib. These helpers are chunk-level and need pandas and pyarrow.
    - Dependencies: pandas and pyarrow only.
    - The loader imports `PARQUET_PATTERN`, `has_telemetry` and `footer_utc`
      back. `storage/sidecar.TELEMETRY_GLOB`'s comment now points at the new
      home.
  - **Readers: new `storage/session_products.py`.** It holds `read_digest`,
    `read_digest_skipped`, `read_fingerprint`, `read_classification` and
    `DIGEST_FILENAME`, `DIGEST_SKIPPED_FILENAME`, `FINGERPRINT_FILENAME`,
    `CLASSIFICATION_FILENAME` and `DERIVED_TAGS_FILENAME`.
    - **This does not follow the item's plain-dict route.**
      `read_fingerprint` and `read_classification` return dataclasses, and
      two readers below `analysis` use their parsed fields. `storage/catalog`
      reads duration, disposition, modes, events, quality and stats.
      `models/evaluation` reads `mode_seconds`, `disposition` and `quality`.
      A plain-dict reader would have made each of them re-parse the sidecar.
      That would give three parsers of one file, and a different answer on a
      malformed sidecar, which `from_dict` rejects whole.
    - So the sidecar types moved down with their readers:
      `SessionFingerprint`, `SessionClassification`, `ModeSegment`,
      `DetectedEvent` and `Disposition`. They are pure data with
      `to_dict`/`from_dict`, and they import nothing from `analysis`.
    - Each module's sidecar `SCHEMA_VERSION` moved with its type, renamed
      `FINGERPRINT_SCHEMA_VERSION` and `CLASSIFICATION_SCHEMA_VERSION`
      because the two share a module. The digest's `SCHEMA_VERSION` and
      `SKIP_SCHEMA_VERSION` stay in `analysis/digest.py`, since no reader
      uses them.
    - `CLASSIFIER_VERSION`, `FINGERPRINT_VERSION`, `is_stale`, computing and
      writing stay in `analysis`, which imports the types back. The seven
      `analysis/events/*` detectors import `DetectedEvent`/`ModeSegment` from
      storage.
    - `storage/catalog.py` imports no `analysis`.
  - **Catalog functions: new `decode/on_read.py`.** It holds
    `can_channel_spellings`, `on_read_catalog`, and `ON_READ_BINDINGS_PATH`.
    `on_read_catalog` reads that constant at call time, and tests patch it,
    so it moved too.
    - Dependencies: `decode.bindings`, `decode.engineering` and `paths`. It
      does not need `can_ids` or `telemetry`.
    - `preprocessing/decode.py` keeps `read_session_decoded*`,
      `decoded_subset_plan`, `decode_on_read`, `on_read_hash`,
      `on_read_can_ids` and `DEFAULT_BINDINGS_PATH`. It imports
      `can_channel_spellings` back.
    - `storage/telemetry_query.py`, `loader`, `analysis/drives` and the rest
      import from `decode.on_read`. `TelemetryQuery` stays in `storage/`.
  - **Sweep.** An AST rewriter retargeted 145 import statements in 106 files
    (`control.sessions` 46 files, `control.capture_manifest` 22,
    `preprocessing.loader` 18, `session_classifier` 15, `preprocessing.decode`
    13, `digest` 8, `fingerprint` 7, `capture_losses` 6, `capture_quality`
    3). Module-level and lazy imports were both covered, as were
    `TYPE_CHECKING` blocks.
    - Docstrings naming the old paths were updated: `session.py`,
      `collection/tags`, `control/{logs,dtc_state}`,
      `analysis/{digest,tune_revisions,behavior_validation,workbench,drives,fingerprint}`,
      `preprocessing/imu_session`, `daemon/health`, `storage/{catalog,telemetry_query,sidecar}`.
    - Current-code docs were updated too: AGENTS.md's two paragraphs, R03's
      Rules and `.claude/skills/engine-management/references/canary-workflow.md`.
      Done-notes in other task files still name the old paths as history.
  - **Patch targets.** No `mock.patch` string named a moved symbol. Three
    module-attribute uses were retargeted:
    - `test_decoded_rows_never_stored`: two
      `monkeypatch.setattr(decode, "ON_READ_BINDINGS_PATH", …)` →
      `monkeypatch.setattr(on_read, …)` on `src.decode.on_read`;
    - `test_derived_store`: `loader.count_session_rows`;
    - `test_fingerprint_featurization`: `fingerprint.SessionFingerprint` and
      `fingerprint.SCHEMA_VERSION`.
  - **Ledger.** `OWED` went from 69 to **45**, the 24 R09.3 entries exactly.
    No other entry cleared, and no new upward edge or pair appeared.
    `LAYERS` is unchanged, because every new module sits in an existing
    package. `grep -rnE "src[./]control[./](sessions|capture_manifest)" src
    tests scripts tools` finds nothing, and the three old files are gone.
  - **`scripts/ml_paths.txt`** adds `src/decode/on_read.py` and
    `src/storage/session_files.py`. Both were `src/preprocessing/` code that
    feeds training (the on-read catalog, session discovery), so they keep
    the tier coverage they had.
  - **Gate.**
    - `ruff check .` and `ruff format --check .` are clean, and `mypy` finds
      no issues in 431 files.
    - `tests/test_layering.py`: 9 passed.
    - Session ordering: the 18 files that exercise `session_started_at` /
      `latest_session` (`test_session`, `test_recorded_sessions`, `test_tags`,
      `test_vehicle_state`, `test_capture_profiles`, `test_trends`, …): 484
      passed. `test_daemon::test_run_daemon_end_to_end_inference_emits_anomaly`
      failed once under `-n 4` and passed alone and in the slow run below
      (the load flake T01 records).
    - Inner-loop tier (`-m "not integration and not slow"`): 6393 passed,
      1 skipped.
    - Slow tests in the 130 affected files: 88 passed, 1 xfailed.
    - `-m integration tests/test_replay_isolation.py`: 1 passed.
    - `-m ml_training`: 122 passed.
  - **Replay.** `tools/replay_event_diff.py ab`, `5f6717b` (`git archive`)
    against a copy of the working tree, session `2026-09-18T213818`, 900 s,
    deployed IF `2026-09-20T054703` + LSTM `2026-09-20T064628`. The tool
    imports no moved path, so it drove both trees unchanged.
    - 901 ticks, 18 events and 2645 score messages on both sides;
    - events identical: 18 of 18 paired, 8316 floats, max |delta| 0;
    - scores identical: IF 0 of 901, LSTM 0 of 872 and fused 0 of 872
      differ, 5255 floats, max |delta| 0;
    - counters and meta identical.

- [ ] **R09.4 `src/signals/`: channel physics out of `analysis`.**
  - **Move to `src/signals/` (layer 0):** `derived`, `committees`, `afr`,
    `fsm`, `pairing` and the `vehicle_signals/` package. Once R09.2 has
    landed, they import only `config`, `decode.engineering`, `timefmt` and
    `columns`.
  - **`analysis/drives.py` → `preprocessing/drives.py`.** It imports
    `storage.sessions` once R09.3 has landed.
  - **`ml_paths.txt`:** add `src/signals/`.

  **Accept:**
  - The ledger entries are gone.
  - `tests/test_analysis_ground_truth.py` passes unchanged: `derived` and
    `afr` feed it.
  - `test_recipe_catalog_golden.py` fingerprints are unchanged.
  - Replay gives an empty diff: `preprocessing/live.py` imports `derived`.
  - The `ml_training` tier passes.

  Size **M**.

- [ ] **R09.5 `src/vehicle/`: the vehicle's own records.**
  - **Move into `src/vehicle/` (layer 1):** `analysis/tune_revisions.py`,
    `analysis/vehicle_ledger.py` and `src/profiles.py`. Nothing below
    `preprocessing` imports `profiles`.
  - **The VIN/CVN probe goes to `collection/`.** `profiles.probe_vehicle_info`
    and `read_vin` transmit through `CanObdClient`. Then either:
    - the profile-resolution functions take the probe result from their
      callers, or
    - the resolution functions move up with the probe.

    Choose whichever changes fewer callers, and record which.
  - **`collection/collectors.py`** imports `active_registry` from
    `control.calibration`, which only re-exports the `profiles` function.
    Import it from `vehicle` directly.

  **Accept:**
  - The ledger entries are gone.
  - The `test_profiles*` and `--profile` CLI tests pass.
  - A replay gives an empty diff, since the collectors are on the serve path.

  Size **M**.

- [ ] **R09.6 `src/behavior/` and `src/features/`, landed together.**
  - **Behaviour models** move to `src/behavior/` (layer 4):
    `decode/behavior_models.py`, `behavior_decoder.py`,
    `behavior_eligibility.py`, `accepted_baselines.py` and `surface_eval.py`,
    which imports `behavior_models`.
  - **Feature assembly** moves to `src/features/` (layer 4):
    `preprocessing/feature_pipeline.py`, `live.py` and `feature_cache.py`.
  - **Why together.** `behavior` imports `preprocessing`, and those three
    files import `behavior`. Moving only one side creates a
    `preprocessing ↔ behavior` pair.
  - **`LocalRangeCondition`**, the spec type `preprocessing/local_range.py`
    takes from `behavior_models`, moves down into `local_range`.
  - **`collection/capture_profiles.model_input_warnings`**, the only
    behaviour-model user in `collection`, moves to `features`. `CaptureProfile`,
    the store and `serving_mode*_channels` stay.
  - **`ml_paths.txt`:** add `src/behavior/` and `src/features/`.

  **Accept:**
  - `decode → preprocessing` is gone, and no `preprocessing ↔ behavior` pair
    exists.
  - Replay gives an empty diff.
  - `artifact_fingerprint` values of the deployed behaviour models are
    unchanged.
  - The ground-truth, recipe-golden and `ml_training` suites pass.

  This is the largest sweep: `feature_pipeline` is referenced from 80 files,
  `behavior_models` from 69, `live` from 55. Check that no other
  session has `feature_pipeline.py` open first. Size **L**.

- [ ] **R09.7 `alerts ↔ models`: fusion belongs to the detectors.**
  - **`alerts/fusion.py` → `models/fusion.py`.** It imports only `config`.
  - **`alerts/feedback.py` → `models/feedback.py`.** It imports only
    `jsonl_store` and `paths`. Its users are `models/retraining_policy.py`
    and `companion/services.py`.
  - **Delete `alerts/emitter`'s lazy `__getattr__` re-export** of
    `IsolationForestInferenceEngine` (left by R01.8) once nothing uses it.

  **Accept:**
  - `alerts → models` is one-way.
  - The pair is gone from `OWED`.
  - Replay gives an empty diff, since the emitter is on the serve path.

  Size **S**.

- [ ] **R09.8 Top of the stack: `daemon`, `dashboard`, `companion`.**
  - **Daemon-owned objects move into `src/daemon/`:** `dashboard/broadcast.py`
    (no imports), `dashboard/behavior_live.py` and `dashboard/ups.py`. The
    dashboard then reads them downward, and these are the home decisions
    R01.8's `STANDING` table asked for.
  - **`control → daemon` (`TYPE_CHECKING` only).**
    `control/profile_capture.py` annotates `HealthMonitor`, and
    `control/sequence_detector.py` annotates `DaemonComponents`. Type them
    against Protocols in `control` that name only the members used.
  - **`companion ↔ dashboard`.** Dashboard routes use `companion.services`'
    job, model-manifest and dataset-health response builders (with
    `_artifact_path`) and `companion.schemas.TrainingRunApprovalRequest`.
    Move those into `control`; they wrap `JobController` and model
    manifests. If one needs `mcp_server.client`, it stays in `companion`,
    and the dashboard stops using it. `companion → dashboard` (the reused
    session routers) then runs one way.

  **Accept:**
  - `STANDING` is exactly `daemon/server.py → dashboard.api`.
  - Both pairs are gone.
  - Replay gives an empty diff.
  - The route-table snapshot (R03.4) is unchanged.

  Size **M**.

- [ ] **R09.9 Close-out: measure, then remove the workarounds.**
  - **`OWED` is `{}`.**
  - **Re-measure** package edges, pairs importing each other (expected 0)
    and function-level cross-package imports, and record before/after here.
  - **Remove the cycle workarounds.** Each function-level import that
    existed only to avoid a cycle becomes a top-level import. Imports
    deferred for import cost (torch, heavy pandas paths) stay; say which is
    which.
  - **AGENTS.md** gets a short "Package layers" paragraph that names
    `tests/test_layering.py` as the source of truth, not a copy of the map.

  Size **S**.

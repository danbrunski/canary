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
  On 2026-09-27 that is `preprocessing/feature_pipeline.py` and
  `models/evaluation_detectors.py`.

## Operator decision this set needs

Record the answer here and never re-open it.

- **R09.1: replace R01.8's named-edge rule with a strict layer order?**
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
| `decode` → `preprocessing.{features,local_range,resampler,session_context,segmenter}` | R09.6 |
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
3. **R09.6 after R09.4,** once `feature_pipeline.py` is clean.
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

- [ ] **R09.1 Turn `tests/test_layering.py` into a layer order.**
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

  Needs the operator decision above. Size **S**.

- [ ] **R09.2 Leaf moves: constants and single-purpose helpers.**
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

  Waits for `models/evaluation_detectors.py` to be clean (it lazily imports
  `plausibility`). Size **M**.

- [ ] **R09.3 `storage` owns the session.**
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
  `behavior_models` from 69, `live` from 55. **Blocked until
  `feature_pipeline.py` is clean** (2026-09-27). Size **L**.

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

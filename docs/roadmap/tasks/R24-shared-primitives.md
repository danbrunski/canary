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

- [ ] **R24.1 A refusal that carries a reason.** Eighteen exception classes
  in `src/` hand-write `self.reason = reason` with their own `__init__`,
  some with `.message`, some with `__reduce__` and some without (an exception
  without it does not survive the trip back from a spawn child with its
  fields). Callers translate one to another by hand in about as many places.
  - [ ] `ReasonedRefusal(Exception)` at layer 0: `reason` (a stable code),
    `detail`, pickle-safe. Existing classes subclass it and keep their names,
    codes and messages.
  - [ ] Extend `tests/test_exceptions_pickle.py` to enumerate every subclass
    by reflection, so a new one cannot skip the round-trip.
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

- [ ] **R24.4 Timezone coercion and ISO formatting.**
  `tzinfo is None` appears on 53 lines in 37 files. `_aware` is defined 8
  times, `_utc` twice more, `_iso` 13 times, and the copies do not agree:
  some `replace(tzinfo=UTC)` (leave a non-UTC aware value alone), some
  `astimezone(UTC)` (convert); `_iso` emits `+00:00` in some files and `Z` in
  others. `src/timefmt.py` has `format_timestamp` and `parse_iso_utc` and
  nothing for this.
  - [ ] `timefmt.as_utc(dt)` (naive is UTC, aware is converted) and
    `timefmt.format_epoch(seconds)`. Migrate the identical copies and the
    eight sites in `features/live.py` first.
  - [ ] For each `_iso` that writes to a stored file, keep that file's
    current suffix behind an argument. Unifying `Z` and `+00:00` in stored
    artifacts is a separate, deliberate change, and probably not worth
    making.

- [ ] **R24.5 Canonical-JSON hashing.** An AST scan found 38 sites that hash
  `json.dumps(…, sort_keys=True)` with sha256, in four flavors: compact
  separators with `default=str` (9, identical to
  `evidence/provenance.hash_json`), compact without (10), default separators
  without (15) and with (4). Stored manifests, cache keys and `meta.json`
  files pin which flavor each site must keep. `plausibility/limits.py`
  records what a hash that silently changes costs: 1 GB of orphaned streams.
  - [ ] `sha256_json(payload, *, compact=True, default=None)` and
    `sha256_bytes` in `src/artifact_hash.py`, which is already "the one
    sha256 function". Migrate the nine `hash_json`-identical sites first: no
    stored hash changes.
  - [ ] The rest move with explicit flags and a one-line comment naming the
    stored artifact that fixes the flavor, with a fixture per caller.
  - Must not change: any stored hash. `test_feature_pipeline_golden` (cache
    key) and the behavior-fingerprint tests are the pins.

- [ ] **R24.6 One bin rule.** `behavior/surface_eval.bin_indices` is the
  rule (R05.10 fixed a right-closed `pd.cut` that dropped left-edge values).
  It is bypassed by: `tuning.py`'s per-band grouping with `pd.cut` and
  default right-closed edges, the R05.10 shape again;
  `tuning_digest._bands`; `workbench._cells` (`right=False`);
  `signals/afr.afr_error_surface`; `np.digitize` in `dyno_inertia` and
  `dyno_power`; `fuel_comp_fit._maf_bins`. A value exactly on an edge can land
  in different cells depending on which page drew it.
  - [ ] `cell_codes(values, edges, *, out_of_range="drop" | "clip")` beside
    `bin_indices`, and an edge-value test (values equal to every edge) run
    against each caller before and after.
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

- [ ] **R24.9 Locked JSONL append.** `_lock_for(path)` with a module-level
  registry is defined three times (`control/brain_jobs.py`,
  `evidence/ledger.py`, `vehicle/vehicle_ledger.py`, one with a non-reentrant
  lock). `brain_jobs` and `evidence/ledger` each also carry an `fcntl.flock`
  append with torn-tail repair and an incremental tail index;
  `models/candidate_store` has a fourth `flock`. `jsonl_store.append_jsonl_line`
  says "callers that need locking take it themselves".
  - [ ] `path_lock(path)`, `append_jsonl_line_locked(path, payload)` and a
    small `TailIndex` in `src/jsonl_store.py`; move `BrainJobStore` and the
    evidence ledger onto them.
  - Durable (fsync) atomic writes are R23.13; this is the append half.
    Pinned by `tests/test_jsonl_store.py`, `test_brain_jobs.py` and the
    ledger tests.

- [ ] **R24.10 One JSON-safe converter.** Nine functions turn numpy,
  dataclass and pydantic values into JSON-safe ones: `_jsonable` in
  `tools/plant_validation.py`, `fuel_plant_check.py`, `manifold_check.py`,
  `replay_event_diff.py` and `src/validation/scenarios.py`; `_json_default`
  in `scripts/lambda_delivery_evidence.py` and `analysis/digest.py`;
  `artifact_lineage.json_default`; and `fuel_comp_report.jsonable`, the
  complete one. They drift: one rounds to six places and handles dataclasses,
  one rounds and handles `model_dump`, one does neither, one has no numpy
  handling. The same value serializes differently across the reports in
  `data/analyses/`.
  - [ ] `src/jsonable.py`: `jsonable(value, *, ndigits=None)`, generalizing
    the `fuel_comp_report` one. Snapshot each tool's JSON before the swap;
    rounding is the per-caller behavior to keep.

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

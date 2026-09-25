# R — refactor review 2026-09-24: large files, repeated code, and the drift bugs inside it

Written 2026-09-24. This is the second whole-repo refactor review. The first
was [`../archive/refactoring.md`](../archive/refactoring.md) (2026-07-18),
and 24 of its 25 tasks landed. Since then **~125k lines have been added and
~15k deleted** in `src/`, `scripts/` and `tools/` (307 commits touching
`src/`), and most of the new debt is in that code.

The review covered six areas in parallel, each read against the July
decisions and the AGENTS.md disciplines:

- collection;
- daemon, control and HTTP;
- analysis and decode;
- models and preprocessing;
- the dashboard frontend;
- scripts, tools and test infrastructure.

This is not a tidy-up list. **Duplication here has already drifted, and more
than thirty of the copies now disagree in ways that are bugs.** Some are
on the serve path; they are listed first below. The structural items come
after the fixes, because the fixes are cheaper and the splits move the same
code.

| File | Scope | Serve path | `ml_training` tier |
|---|---|---|---|
| [R01](R01-shared-primitives.md) | One home each for session names and ordering, data paths, coercion, sidecars, sha256, operating constants; a layering test | parts | R01.2 |
| [R02](R02-collection.md) | slcan parse errors, the Mode 22 window predicate, preview/live plan parity, frame timestamps, responder pinning, recorders; then the 727-line `run_can_obd_loop` and the 3,137-line `obd_scheduler` | yes | no |
| [R03](R03-daemon-control-http.md) | One vehicle-state classifier; bounded classification; the delete guard; a route snapshot; then the capture lifecycle and per-domain `APIRouter`s out of the 1,640-line `register_control_routes` | yes | no |
| [R04](R04-models-and-preprocessing.md) | FP benchmark and scoring-rule parity, the suffix parser, one decode path, promotion-gate silent passes; then health out of `models/`, the IF/sequence trainer splits | yes | most items |
| [R05](R05-analysis-and-decode.md) | One tuning context (registry, stoich, noise floor), refusing significance, `compare_channels` for cam tracking, derived-signal table, fingerprint featurization, recipe clones; then `fit_recipe`, decode slices, surface eval | parts | no |
| [R06](R06-scripts-and-tools.md) | A shared bus-tool module (two tools skip the passive check), a CLI snapshot, `retrain.py` phases, library code out of `scripts/` | no | R06.4 |
| [R07](R07-test-infrastructure.md) | `src → tests` import, test writes into the repo tree, ScriptedEcu for collector fakes, one global-state reset, shared builders | no | no |
| [R08](R08-dashboard-frontend.md) | Undefined theme class, raw error text, a localStorage crash, then API layering, formatter sweep, the 840-line `CaptureProfilesPanel`, one palette, one poll loop; ESLint + Prettier with a ratcheted baseline (R08.13) | UI | no |

## Fix first: drift bugs found by the review

Every item below starts with a test that fails before the fix. "Verified"
means the orchestrator re-read the code on 2026-09-24 and confirmed the
mechanism. "Reported" means a reviewer traced it with file-level evidence and
it has not been independently re-checked; the failing test is the check.

| Item | What goes wrong | Where | Status |
|---|---|---|---|
| R03.12 | IF and sequence score and emit on tuning captures. The gate checks only vehicle state, and the Xterra `tuning` profile polls Mode 01 RPM. The operator decision (below) says they must not | daemon | verified |
| R04.6 | The A02 evaluation (`evaluate_detectors`) scores tuning captures as drives ("captures included") | offline | verified |
| R03.1 | During an all-DID capture, `/status` says `engine_running` but the inference gate says `engine_off`: it lacks the broadcast-RPM fallback. Land it after R03.12, which is what should keep tuning captures unscored | daemon | verified |
| R02.1 | An slcan parse error during a discovery probe propagates as `ValueError`, which `run_with_reconnect` re-raises as a config error | collector | verified (chain); exit reported |
| R03.2 | `POST …/classification` loads a whole session twice in-process: no child, preflight, deferral or single-flight. MCP agents can reach it | daemon | verified |
| R03.3 | `DELETE /control/sessions/{id}` guards only the active capture, not the writer's live session | daemon | verified |
| R05.1 | Flashes recorded from the dashboard go to the operational registry, which the digest, MCP tools and ROM-maps route never read. Stoich is hard-coded at 14.7 in 12 signatures | digest child, MCP | verified |
| R01.1 | `scripts/tag.py` with no `--session` tags the *previous* session during a capture. `_tuning-*` sessions 404. Catalog rows for captures get `started_at = NULL` | CLI, HTTP | verified (tag, `_tuning-`); catalog reported |
| R01.2 | `build_feature_frame` loads DIDs from a working-directory-relative path with no parameter, and the cache key hashes a different constant | training | verified |
| R04.1 | The FP benchmark scores every channel with no scored-column mask and no family path, then publishes an FPR to the ledger for a detector the daemon doesn't run | offline | verified (all-channel); rest reported |
| R04.5 | The promotion gate `continue`s past a missing recall, so a withheld candidate detector passes non-regression | offline gate | verified |
| R05.2 | `NoiseFloor.conditions` defaults to `"unspecified"`, contrary to AGENTS.md. Three inline significance copies ignore conditions | analysis | verified (default) |
| R05.4 | `DERIVED_CHANNEL_INPUTS` covers 4 channels, so input warnings never fire for 9 of the 19 recipes | profiles | verified |
| R05.7 | The recipe catalog's trailing loop overwrites every recipe's minimums, so none can be set per recipe | refit | verified |
| R06.1 | `probe_local_identifier.py` and `scan_constant_dids.py` transmit with no passive check | tools | reported |
| R07.2 | Tests write into `data/control/` (trip odometer, catalog) and can write `models/` and `data/analyses/` | tests | verified (mtime) |
| R08.1 | `text-fg-muted` is undefined in the theme, so 17 "muted" notes render at full colour | UI | verified |
| R08.3 | `TestSessionPanel` reads and writes `localStorage` with no guard, so the panel crashes where storage is blocked | UI | verified |

Reported but not re-checked, each behind its own failing test:

- R02.2: memory reads mis-windowed.
- R02.3: preview/live DID-rate precedence.
- R02.4: stale kernel stamps.
- R02.5: `obd_request` accepts any 0x7E8–0x7EF reply.
- R02.6: `record_can_obd` loops on a full disk.
- R02.7: `record.py` ignores batching config.
- R02.9: the exit snapshot reports configured batch sizes.
- R03.4: `/ws/control` compares the token with `!=`.
- R03.5: `loss_epoch_start` is `None` after a shutdown.
- R04.3: rolling-suffix copies.
- R04.4: live vs storage decode metadata.
- R04.7: fast/slow gap rule.
- R04.13: harness wiring.
- R05.3: cam-tracking `corrcoef`.
- R05.5: fingerprint featurization.
- R05.6: session-deviation aggregates.
- R05.10: `linspace` edges drop the minimum.
- R06.2: whitespace override reasons.
- R08.2: the SessionChooser race and the shared TimingView refusal.
- R08.9: overlapping polls.

## Order

1. **The serve-path drift bugs.** R03.12 then R03.1, R02.1, R03.2, R03.3,
   R05.1, then R02.2–R02.7. R04.6 can go alongside, since it is offline.
   Each is small and independent. Serve-path items replay a
   pinned session before and after.
2. **R01, the shared homes.** Most later items delete copies into these
   homes, so they go before the area work that uses them. R01.1 and R01.2
   carry bugs of their own.
3. **The safety nets, before any big move:**
   - R03.4, the route-table snapshot;
   - R06.2, the script CLI snapshot;
   - R05.7's recipe golden test;
   - R07.1, R07.2 and R07.4 for test isolation.
4. **The offline correctness items.** R04.1, R04.5, R05.2–R05.6, R06.1,
   R08.1–R08.3.
5. **The structural splits.** These need items 1–4 in place first:
   - R02.9 and then R02.10, the collector loop and the scheduler package;
   - R03.5 and then R03.6, the capture lifecycle and the routers;
   - R04.2, R04.8–R04.11;
   - R05.8–R05.13;
   - R06.3 and R06.4;
   - R07.3 and R07.5;
   - R08.4–R08.9.
6. **The small sweeps.** R02.14, R03.11, R05.14, R06.6, R08.10–R08.12,
   whenever someone is in the file.

No task here changes the order in `OVERNIGHT-QUEUE.md`. If this set is queued
for overnight work, the orchestrator puts steps 1–3 in the queue as their own
entry, and the operator decisions below are made first.

## Operator decisions this set needs

Record each answer in the item and never re-open it.

- **R01.1:** does a `_tuning-<id>` directory count as a session for HTTP
  reads and sidecars?
- **R03.12:** should `corpus_eligible: true` stay possible for a
  non-default profile? Do behavior-model deviations and plausibility checks
  also stop during tuning captures?
- **R03.1:** which freshness rule is right for the broadcast-RPM fallback?
  The live copy requires CAN freshness; the Parquet copy does not.
- **R03.11:** keep the `--collection-only` drain on the storage sink, or
  retire it?
- **R05.3:** converge delivery error onto the acquisition-paired
  `match_ordered`? The numbers shift.
- **R05.5:** fingerprint featurization requires re-freezing the trend
  reference epoch. When?
- **R05.7:** move the DID-named behavior recipes into a per-profile Python
  module?
- **R06.5:** fold `detector_benchmark` into `evaluate_detectors`?
- **R08.6:** which DID rate is the "requested" rate in the capture table?
- **R08.13:** add a TS formatter and linter? **Decided 2026-09-24: yes.**
  ESLint (type-aware) + Prettier, with a bulk-suppressed baseline; see
  R08.13.

## Shared acceptance rules

- **Behaviour-preserving unless the item names the change.** A dedupe that
  changes an output says which caller sees it and why the new output is
  right.
- **Serve path means replay.** An item touching code the live daemon runs
  replays a pinned session before and after, and records the event and row
  diff in the item. Replay cannot establish hardware throughput. Items that
  claim a Pi-side effect stay `[~]` until measured on the Pi.
- **Old import paths keep working.** They re-export from the new home until
  every caller has moved. The item lists the monkeypatch targets it
  retargets.
- **Each item has its own commit, with explicit pathspecs.** Other sessions
  share this tree, and no tree-wide destructive git is allowed. Push through
  `scripts/push.sh`, then confirm CI.
- **Tier cost is known up front.** R04 items and R01.2 run `ml_training` on
  push. Batch them.
- **`[x]` only with evidence:** the failing test that now passes, the replay
  diff, or the snapshot unchanged.

## Relation to existing checklists

- **A01/A02/A03:** R04.1 and R04.2 make the offline benchmark and the
  calibration side score the rule A01/A03 put on the serve path. A02.4 said
  to "reuse `false_positive_benchmark` where it fits", and R04.1 is why it
  does not fit yet.
- **B02/B03/B05:** R05.7 and R05.8 restructure `default_recipes` and
  `fit_recipe`, which B02 and B05 extend. Land one side at a time. The
  golden test in R05.7 is the contract both sides respect.
- **T01:** R07.3 removes the last real-clock collector fakes, the shape
  T01.1–T01.8 fixed one test at a time. Re-run T01's load check after it.
- **W-series:** R08 cleans up the code the W pages added. It does not change
  what they show.
- **H01 (source policy):** unaffected. R01.8's layering test must not forbid
  the imports H01 relies on.

## Decisions carried forward (don't re-open)

- **Tuning logs are not in the anomaly detectors** (operator, 2026-09-24).
  They don't represent the default profile, so IF and sequence neither train,
  calibrate, evaluate nor score live on a non-corpus-eligible capture.
  Training was already protected by `_capture-*` storage. R03.12 closes live
  scoring, and R04.6 closes evaluation. Behavior models and plausibility
  checks are separate calls, recorded as questions in R03.12.

- The collection `decode_*` functions and the φ/λ decoder pair stay
  separate. The ISO-TP *assemblers* stay as they are. R02.5 shares only the
  receive-loop *policy*, and says what changed since July.
- `_MODE01_SPEC` and the refit recipe catalog stay typed Python, not YAML.
- `isolation_forest.py` is not split as a whole file (28 importers). Only
  its calibration cluster may move (R04.9).
- `_torch_module` copies raise distinct exception types and stay separate.
- `RankedBars`/`ChannelBars`, `api/http.ts`'s one-function-per-route shape,
  and `scripts/ups_shutdown_watch.py` as a single file all stand.
- The 15 hand-written MCP tools stay (D03.23).
- Transports stay symmetric. Nothing here deletes the ELM327 path.

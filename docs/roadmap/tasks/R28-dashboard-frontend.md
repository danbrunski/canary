# R28 — Dashboard frontend: render rate, API contracts, ROM Editor structure

Part of the 2026-11-12 review ([R22](R22-refactor-README.md)). Scope:
`src/dashboard/static/` (React 18, Vite, Tailwind v4, no chart library;
43.6k non-test lines, 75.2k with tests). Counts are review snapshots; locate
code by symbol. Nothing here has been implemented. Shared acceptance rules are
in R22.

**State of the area.** Type hygiene is clean: `eslint-suppressions.json` is
empty (baseline cleared in `26948452`), there are no `eslint-disable`,
`@ts-ignore` or non-null assertions in non-test code, and three
`as unknown as`. R19's Logs and Analysis split held (pages are compositions,
views ≤ 330 lines) and so did R08.7's single palette. The debt is in how often
the app renders, in hand-copied API types, and in the ROM Editor tree, which
neither earlier review covered.

- [~] **R28.1 Every WebSocket telemetry message re-renders the whole app.**
  `hooks/useTelemetryStream.ts` `onMessage` calls
  `setState({channels: new Map(channelsRef.current), …})` once per message.
  The daemon publishes per record, per decoded CAN row and per DID
  (`src/daemon/drain.py` `run_telemetry_broadcast_sink`), not per tick.
  `App` rebuilds its data object on every state change and passes `channels`
  to about ten Workbench pages; 31 panel files take it. `memo` appears once in
  the tree (`SimMaps.tsx` `SimMap`), so pages that never read a channel
  re-render too, including the ROM Editor's 500-line `Workspace` while the
  operator is editing. No `requestAnimationFrame`, `startTransition` or
  `useDeferredValue` anywhere in non-test code.
  - [~] Measure first: messages per second on a connected tab during a
    `tuning` capture, and commit count from the React Profiler on the
    Pi-served build on a phone. Record both here. The structure is verified;
    the cost is not.
    Messages per second, measured offline (2026-11-12) from stored tuning
    sessions, since `run_telemetry_broadcast_sink` publishes every record plus
    its decodes and the stored session holds the same rows (`can` raw +
    `decoded` + `obd` + `gps` + `derived`): `_tuning-2026-08-05T142720`
    (4,315,958 rows over 52 min) median 2,003 rows/s, p95 2,177, p99 2,231,
    max 3,238; `_capture-tuning-hybrid-research-2026-09-17T224717`
    (5,610,717 rows over 45 min) median 2,435 rows/s, p95 2,448, max 2,459
    (`can` ~1,680/s, `decoded` ~690/s, `obd` ~44/s). That is ~2,000-2,450
    `setState` calls and Map copies a second before this change, against
    ~60 a second after it. Upper bound: a tab sees only what the broadcaster
    delivers, and it drops oldest on backpressure.
    NOT measured: the live message rate on a connected tab, and the React
    Profiler commit count on the Pi-served build on a phone (no Pi or phone
    from this session). Left open for a drive.
  - [x] Coalesce in the hook: keep ingesting into the ref, flush to state at
    most once per animation frame (or a stated interval). Pin with a
    fake-timer test: N messages inside one window make one state update.
    `useTelemetryStream.ts`: `ingest` writes the ref and marks it dirty;
    `schedule()` arms one `requestAnimationFrame` and, racing it, a
    `FLUSH_FALLBACK_MS` (250 ms) timer, and whichever fires first flushes and
    cancels the other. The fallback is for a hidden tab, where rAF is paused:
    the audible alarms in `AlarmsPanel` evaluate `channels`, so a hidden tab
    must still update (browsers clamp its timer to ~1 Hz). A connection change
    flushes at once, with any pending channels, and cancels the pending frame;
    unmount cancels both. The Map is copied only when a channel changed.
    Pinned in `useTelemetryStream.test.ts` ("frame coalescing", fake timers):
    1,000 messages in one window give exactly one render and the last value
    per channel; a spent frame gives no further render; a second window gives a
    second render; the timer fallback fires at 250 ms with rAF stubbed dead;
    unmount leaves zero timers and no render; a reconnect state change flushes
    at once with no frame left behind; a connection-only change keeps the Map
    identity. Mutation check: flushing per message fails 3 of them.
  - [~] If the measurement still shows whole-tree commits: expose a
    `useChannel(key)` subscription (`useSyncExternalStore`) so only readers of
    a key re-render, and `memo` the Workbench pages that take no channels.
    Done in the smaller form that removes the cost, not the per-key store
    (2026-11-13; the Pi-build Profiler count above is still owed, so this was
    judged on structure and an offline render count). Coalescing left every
    Workbench page re-rendering at up to 60 a second because `App` hands
    `Workbench` a new `data` each commit and nothing was memoised.
    (1) `panels/Workbench.tsx`: a `Pure` table holds `memo(...)` of the 32
    pages that read no channel (every `renderItem` case except `SystemPanel`,
    `DataSourcesPanel`, `TpmsPanel`, `ChartsPanel`, `AlarmsPanel`,
    `CustomDashboardPanel`, `TripPanel`, `LoggingPage`, `RomEditorPage`). Their
    other props are stable between the updates that matter to them:
    `useCompanionSettings` now returns a `useMemo`'d object with `useCallback`
    setters (it built a new object every render, which would have defeated the
    memo on `ControlPanel`, `TrainingPanel` and `SessionsPanel`).
    (2) ROM Editor: `RomEditorPage` hands `channels` to its live overlay through
    `hooks/telemetryChannels.ts` (`TelemetryChannelsContext` and
    `useTelemetryChannels`); `LiveOverlay` reads it and no longer takes a
    `channels` prop; `Workspace` is `memo` and takes no channels, so the
    500-line tree stays out of the frame-rate render and only the overlay (the
    one thing that shows a channel, mounted behind a toggle) re-renders.
    Not done: a per-key `useChannel` store. Channel readers (the nine pages
    above) still render once per frame, which is what they exist to do; a
    key-level store would cut that further and is the next step only if the
    Pi-build Profiler shows readers are the cost. `README.md` (state
    management) says where a new channel-free page goes.
    Offline render counts, with the real `useTelemetryStream` under fake timers
    and a mocked socket, 20 frames of 100 messages (2,000 messages), counting
    commits at a `Profiler` around `Workbench` and renders of the page
    (`panels/WorkbenchRenders.test.tsx`):
    per-message flush (before `ca4d64bb`): 2,000 commits, 2,000 renders of every
    page (the old hook's one `setState` per message, by construction);
    after coalescing alone (`ca4d64bb`): 20 commits, 20 renders of Control, a
    page that shows no channel (measured by taking `memo` off Control:
    `expected 20 to be +0`); after this change: 20 commits, 0 renders of
    Control, 20 of Alarms (a reader, as it must). ROM Editor
    (`panels/tuning/RomEditorRenders.test.tsx`): 50 new `channels` Maps give 0
    further `Workspace` renders with `memo`, 50 without (`expected 51 to be 1`).
    Both new tests fail with the memo removed. Not measured: milliseconds per
    commit, which needs the Pi-served build on the phone.
  - Must not change: what is stored per channel, the seed-then-stream order,
    `useTelemetryStream.test.ts`.
  - [x] Found while verifying: `AlarmsPanel` keyed its 1 s evaluation interval
    on `channels`, so every telemetry update re-armed it. Per message before
    this item, and per frame after it, the tick never fired while data
    flowed, so no threshold alarm could fire during a drive. The tick now
    reads a `channelsRef`, and the interval depends only on the rules and the
    audible switch. `AlarmsPanel.test.tsx` (new) streams a fresh Map every
    50 ms for 5 s and expects a fired event. It fails on the old effect and
    passes on the new one.

- [ ] **R28.2 API response types are hand-copied from Python and have
  drifted.** `src/types/*.ts` holds 402 exported types in 4,906 lines;
  `api/http.ts` has 97 route functions and `fetchJson<T>` is an unchecked
  cast, so drift is silent. The backend declares no `response_model`, so there
  is nothing to generate from (R08 declined codegen, and that still holds).
  Two Python tests pin a TS type today (`tests/test_brain_job_kinds_ts.py`,
  `tests/test_rom_levers_parity.py`). Mismatches found by spot check:

  | TS type | Missing or wrong | Python source |
  |---|---|---|
  | `AnomalyEvent.source`, `ScoreMessage.source` (`types/live.ts`) | 4 literals; Python has 9 (`dtc`, `trend`, `misfire`, `plausibility`, `committee` missing). `AnomalyPanel`'s source filter hard-codes the 4 and `StatusStrip`'s ternary has no arm for the rest. `dedupe_key` is missing too | `src/alerts/events.py` `AnomalySource`, `AnomalyEvent` |
  | `StatusHealth` | `model_version_mismatches`, `sequence_suppression`, `sequence_cadence_refusal`, `detector_roles`, `capture_lifetime`, `telemetry_sinks` | `src/daemon/health.py` `HealthMonitor.snapshot` |
  | `StatusResponse` | `tuning_mode`, `bus_budget`, `startup` | `src/dashboard/status.py` `to_dict` |
  | `health.system` | typed `Record<string, number \| null>`; carries a bool and a string | `src/cgroup_memory.py` `memory_limit_status` |
  | `CaptureProfile` (`types/control.ts`) | `kline`, `did_cycle_seconds`, `max_tuple_skew_s` | `src/collection/capture_profiles.py` `CaptureProfile` |
  | `lib/romSim.ts` `MAX_GRID_POINTS`, `MAX_DRAWS` | restated constants, no parity test | `src/analysis/rom_editor.py` |
  | `CaptureProfileEditor` `blankProfile()` | `round_timestamps: true`; the Python default is `False` | same |

  `model_version_mismatches` is the field that says a detector is serving
  against the wrong manifest, and the dashboard cannot show it.
  - [ ] Fix `AnomalyEvent.source` first: one exported const for the source
    list, used by the type, the `AnomalyPanel` select and `StatusStrip`.
  - [ ] Add a key-set contract test in the shape of
    `test_brain_job_kinds_ts.py`: build the real payload in Python
    (`StatusSnapshot.to_dict`, `HealthMonitor.snapshot`,
    `CaptureProfile().model_dump()`, `get_args(AnomalySource)`), parse the
    interface's field names and literal unions out of the `.ts` file, assert
    equal sets (a `?` field may be absent). Start with the rows above, then
    widen one type at a time.
  - [ ] Decide `blankProfile()`'s `round_timestamps` on purpose and pin it.
  - [ ] Confirm a saved profile that carries `kline` round-trips through
    `CaptureProfileEditor` (it clones and spreads, so it should; unverified).

- [~] **R28.3 Hand-rolled `setInterval` polling has regrown beside
  `usePolling` (R08.9).** `panels/SessionsPanel.tsx` and
  `panels/ControlPanel.tsx` each carry the same block,
  `void load(true); const timer = window.setInterval(() => void load(false), 5000)`,
  with a `cancelled` flag, no abort and no in-flight guard: a slow response
  overlaps the next request, which is the bug R08.9 removed. Also
  `panels/RescoreControl.tsx` (job poll) and `hooks/usePairing.ts`.
  Six panels run their own 1 s clock (`StatusStrip`, `ReplayBanner`,
  `ReplayCard`, `NetworkPanel`, `TestSessionPanel`, `TuningContextBar`) beside
  `hooks/useNow.ts`, and `romEditor/SimForm.tsx` defines a private `useNow`
  identical to the shared one.
  - [x] Move the two panel reads to `useControlResource(fetcher, {intervalMs})`,
    keeping "first read reports, re-reads stay quiet". `SessionsPanel` and
    `ControlPanel` read through `useControlResource(…, {intervalMs: 5000,
    enabled: Boolean(token)})`; the new `hooks/useFirstFailure` wraps the read
    so only the first failed read of a (token, captures) pair sets the notice.
    Tests (fake timers): `SessionsPanel.test.ts` and `ControlPanel.test.tsx`
    "poll timing" blocks pin first read immediate, next 5 s after settle (4999
    ms none, 5000 ms one), no overlap with a never-answering read, first
    failure reported / re-read failure quiet, no read after unmount;
    `useFirstFailure.test.ts` (4). Deliberate differences, both the point of the
    item: the cadence is 5 s after a read *settles* (was a fixed `setInterval`),
    and a failed post-action refresh no longer
    replaces the action's "completed" notice (below).
    Ordering: `usePolling`'s `refresh()` returns a `Promise<RefreshOutcome>` that
    settles when the read it starts settles, and also when a newer read
    supersedes it, the hook is disabled, or it unmounts; it never rejects. The
    outcome is `{status: "ok"}`, `{status: "failed", error}` or
    `{status: "abandoned"}` (superseded, disabled, unmounted, aborted: never a
    failure).
    Every post-action caller returns/awaits it (`ControlPanel` and
    `SessionsPanel` `then:`, `TestSessionPanel.refreshBoth`,
    `CalibrationPage.refresh`, and the `await refresh()` tails of the actions in
    `NetworkPanel`, `PowerPanel`, `ProfilesPanel`, `TuningPanel`, `UpdateCard`,
    `PairedDevicesPanel`, `VehicleLedgerPage`, `TreadWearCard`, `TrainingPanel`,
    `BrainPage`, `CompanionConfigCard`, `AnomalyPanel`, `RomEditorPage`,
    `EcuBackupCard`), so `busy` holds, and "completed" waits, until the panel
    shows the new state. Without it Capture start re-enabled for a round trip
    while `capture` was still null, and a second click got a 409. Pinned by
    `ControlPanel.test.tsx` "holds a capture start busy until the follow-up read
    lands" (fails with `then` not returning the refresh) and the four `refresh
    promise` tests in `usePolling.test.ts`. **Operator 2026-11-15: a failed
    follow-up read is loud.** The action went through, so the panel says so in
    its existing action-error slot: "Done, but the refreshed view could not be
    loaded: <describeError>" (`FollowUpReadError`), instead of "<label>
    completed." and a silently stale view. Background poll re-reads stay quiet
    (`useFirstFailure`); only the read an action awaits is loud. It lives in
    one place, `hooks/followUpRead.ts`: `useAsyncAction`'s `then` applies
    `requireFresh` to whatever it returns (so `then: refresh` needed no change
    in `ControlPanel`, `SessionsPanel`, `CustomPidsPanel`, `RomLibraryPage`,
    `CaptureProfileEditor`, and `TestSessionPanel`/`CalibrationPage`, whose
    two-read refresh now returns `combineOutcomes`), and a hand-rolled action
    writes `await reloaded(refresh())` inside the try that already shows its
    error (`NetworkPanel`, `PowerPanel`, `ProfilesPanel`, `TuningPanel`,
    `UpdateCard`, `PairedDevicesPanel`, `VehicleLedgerPage`, `TreadWearCard`,
    `TrainingPanel`, `BrainPage`, `CompanionConfigCard`, `RomEditorPage`).
    Handled differently: `EcuBackupCard` reads the outcome in its `finally`
    (`followUpFailureText`) so the controls still re-enable; after a start only
    when the POST succeeded (a refused start shows its refusal, never "Done,
    but"), and a cancel keeps its own error over the read's; `TreadWearCard`
    renders `saveError` in its early-return error branch too, since a failed
    read turns the whole card into the error text; `AnomalyPanel` had no action-error slot and gained a
    `Notice` above the verdict row, and the verdict's optimistic mark stays
    (the POST succeeded); `ProfilesPanel.onEdited`'s fire-and-forget read now
    sets the notice too. `PowerPanel`, `TuningPanel`, `ProfilesPanel` and
    `PairedDevicesPanel` had their `Notice` inside `ResourceGate`, which a
    failed read turns into its error text and so hid the action's message; it
    moved above the gate. `followUpSources.test.ts` fails on any `refresh…()` call in `panels`,
    `hooks` or `components` whose outcome is discarded (bare `await`/`void`,
    inside `Promise.all`, a `.then`/`.catch` chain, an arrow body), unless it is
    handed to `reloaded`/`combineOutcomes`/`followUpFailureText`/`requireFresh`
    or returned/assigned; three background or manual re-reads are listed with a
    reason (`useStatus`'s `refreshFull`, `ControlPanel`'s Refresh button,
    `TestSessionPanel`'s re-run preflight button).
    Pinned by `usePolling.test.ts` "refresh outcome" (ok, failed with the raw
    error, superseded, unmount, disabled and aborted are not failures),
    `followUpRead.test.ts`, and one "says so when the read after ... fails"
    test each in `ControlPanel.test.tsx`, `PowerPanel.test.ts`,
    `VehicleLedgerPage.test.tsx`, `TreadWearCard.render.test.tsx` and
    `EcuBackupCard.test.tsx` (each fails with `requireFresh`/
    `followUpFailureText` made no-ops; the tread test also fails without the
    `saveError` line, the ECU refused-start test without the `started` guard).
    Still quiet, see the open sub-item below.
    Settle guarantee, also pinned: a `refresh()` in the same batch as the
    unmount settles (`usePolling.test.ts`, fails without the unmount drain);
    `delayFirst` with `intervalMs: null` is ignored, so a one-shot fetches at
    once instead of never settling.
    **Waits now declared.** Only `ControlPanel` and `SessionsPanel` already
    held busy for their follow-up read (`then: refreshX`). Sixteen panels used
    to fire `refresh()` and forget it and now hold busy until it settles:
    `NetworkPanel`, `PowerPanel`, `ProfilesPanel`, `TuningPanel`, `UpdateCard`,
    `PairedDevicesPanel`, `VehicleLedgerPage`, `TreadWearCard`, `TrainingPanel`,
    `BrainPage`, `CompanionConfigCard`, `AnomalyPanel`, `RomEditorPage`,
    `EcuBackupCard`, `TestSessionPanel` (`refreshBoth`) and `CalibrationPage`.
    Two read differently from the rest: `EcuBackupCard`'s await sits in
    `finally`, before `setBusy(false)`/`setConfirming(false)`, so the card stays
    busy and the confirm stays open until the read lands; `AnomalyPanel` clears
    the hypothesis and question fields only after the read. Exposure: `fetchJson`
    has no timeout, so a Pi that dies after a successful POST keeps these panels
    busy until the browser gives up on the request.
  - [ ] Control reads have no timeout; a hung follow-up read holds busy (the
    waits above make every action's busy state as long as its read). Decide a
    timeout in `api/core` (or a per-call `AbortSignal.timeout`); not done here
    on purpose. A timed-out follow-up read would now surface through the loud
    path above (a timeout that rejects the fetcher is a `failed` outcome; one
    that aborts it would read as `abandoned`, so use a rejecting timeout).
  - [ ] Four fire-and-forget follow-up reads are still quiet: the child
    callbacks `DiagnosticFunctionsPage` `onDone`, `TrainingPanel` `onApproved`
    and `RomLibraryPage`/`EcuBackupCard` `onLibraryChanged` (typed
    `() => void`, called after the child's own action), and `TestSessionPanel`'s
    "re-run preflight" button (a manual re-read, deliberately listed in
    `followUpSources.test.ts`). Make the first four take the outcome (or a
    `reloaded` promise) and show it in the child's notice slot.
    Known holes in the `followUpSources.test.ts` guard, found verifying
    2026-11-15 (nothing in `src` uses them today): `const p = refresh();` with
    `p` unused passes (`=` counts as consumed; ESLint `no-unused-vars` catches
    the unused name); `refresh?.()` is not matched; a handler prop
    (`onDone={refresh}`) is invisible to it, which is where the four callbacks
    above live. One false positive: `then: () => refresh()` is flagged (write
    `then: refresh`). Close the handler-prop hole with the callbacks.
  - [x] `RescoreControl` becomes a `usePolling` enabled while the job runs.
    New `usePolling` option `delayFirst` keeps the old first look one 2 s
    interval after the submit; `getJob`/`getJobResult` take an optional signal.
    Tests: `RescoreControl.test.ts` "job poll timing" (first look at 2000 ms not
    1999, chained 2 s, no overlap, stops on success/failure/unmount);
    `usePolling.test.ts` (delayFirst). `hooks/usePairing` also rides
    `usePolling` (immediate first poll, 1 s, every outcome handled in the
    fetcher so it still stops on any failure/approval): `usePairing.test.ts`
    (4) plus the existing `PairingCard.test.tsx` timing.
  - [x] Replace the six clocks and `SimForm`'s copy with `useNow`. `useNow`
    gained `enabled` (default true) so `NetworkPanel` (window open),
    `ReplayBanner` (replay running) and `TuningContextBar` (capture active)
    keep their conditional tick; `useLiveOverlay`'s 250 ms clock uses it too.
    `useNow.test.ts` (4: 1 s default, custom interval, disabled = no timer,
    unmount).
  - [x] Make it stay fixed: the `dashboard-setinterval` rule in
    `tests/test_source_rules.py` (already present, allowlist) fails on
    `setInterval` outside `hooks/`; its allowlist is cut to `AlarmsPanel.tsx`
    (an alarm-rule evaluator tick, not a poll or clock). `pytest
    tests/test_source_rules.py -n0`: 32 passed. Gate: `pnpm typecheck`, `pnpm
    lint`, `pnpm format` clean; `pnpm test` 138 files / 1474 tests passed;
    `pnpm e2e` 46 passed, 3 skipped.

- [x] **R28.4 A shared control-route test harness, before R28.5.** The same
  `route()` mock keyed on `` `${method} ${path}` `` is hand-written in six
  test files (`RomEditorSimulate`, `RomEditorPage`, `RomLibraryPage`,
  `CalibrationPage`, `EcuBackupCard`, `PairedDevicesPanel`), with variants in
  three more; 19 files `vi.mock("…/api/core")`; `cell()`, `bodyOf()`,
  `renderEditor()` and one `afterEach` are each copied two to four times.
  `RomEditorSimulate.test.tsx` is 1,878 lines and 73 tests.
  Done 2026-11-15.
  - [x] `src/test/controlRoutes.ts`: `routeControl(table, {fallback})`,
    `bodyOf`, `callsTo(path)`, `click`, `type`, `renderRomEditor(sha)`,
    following `test/harness.ts` `mockHttp`. A value resolves, an `Error` rejects
    (so "this route fails" is `"GET /control/rom/backup": new Error("pi went
    away")`), a function is called with `{init, key}` and answers by the same
    rules; `sequence(first, new Error(…))` is a read that works once and then
    fails, the follow-up read of an action (R28.3). Also `jsonOf`, `sentTo`,
    `allCalls`, `select` and `resetControlRoutesAfterEach` (the one `afterEach`:
    mock calls, fake timers, URL, `localStorage`); `cell()` joined
    `test/romEditorViews.ts`. An unlisted request rejects `unexpected METHOD
    path`, so a page that starts asking for something new fails the test that
    did not expect it. `vi.mock("…/api/core")` itself stays in each file (it is
    hoisted per file; the same constraint `mockHttp` documents), so the 19
    mocks remain and the 13 hand-written `control.mockImplementation` routers
    in nine files are gone: `CalibrationPage` (1), `EcuBackupCard` (3, two of
    them the failed-follow-up read, now `sequence`), `RomLibraryPage` (1),
    `RomEditorPage` (1), `RomEditorSimulate` (2), and the variants
    `TuningContextBar`, `RomEditorTables`, `RomEditorOverlay` (2),
    `RomEditorRenders`. Left as they are, on purpose: `PairedDevicesPanel` stubs
    `fetch` with a stateful fake (a different seam, not a route table), and the
    path-only fixtures of `AnalysisPage`, `LogViewer`, `SessionChooser`,
    `DynoView`, `TimingView`, `DeliveryView` (no method in the key). The
    `api/http` named-function tests (`ControlPanel`, `PowerPanel`,
    `VehicleLedgerPage`, `TreadWearCard.render`) already say "this read rejects"
    with `mockRejectedValue` and need no route table. Pinned by
    `test/controlRoutes.test.ts` (11 tests: routing by method, unlisted
    rejects, Error rejects, falsy values resolve, function and thrown answers,
    fallback, `sequence`, `callsTo` / `sentTo`). Mutation-checked, each
    restored byte-identical: `followUpFailureText` returning null fails the
    `EcuBackupCard` failed-follow-up test; the `CalibrationPage` verify
    `record_revision` forced to null fails `needs a carry reason before
    recording`; the simulate path `/simulate` to `/simulat` fails 8 of the
    `RomEditorSimulate` cancel and timing tests; the trace path `/trace` to
    `/traces` fails all 26 `RomEditorOverlay` tests; `routeControl` no longer
    rejecting an `Error` answer fails 8 tests across `EcuBackupCard`,
    `CalibrationPage` and `controlRoutes.test.ts`. `layering.test.ts` lets
    `src/test/` name `controlJson` and gains "imports test support only from
    tests and from test support" (fails on a `lib/` file importing `../test/…`).
  - [x] Split `RomEditorSimulate.test.tsx` along its existing `describe`
    blocks (presets and cost, cancel, typical values, timing). Now
    `RomEditorSimulate.test.tsx` (710 lines: the form, grid edges, WOT column,
    Run, no-result, remembered values, seed, the view), `.presets` (326),
    `.cancel` (125), `.typical` (519), `.timing` (182); the shared routes, form
    helpers and set-up are `test/romSimulateKit.ts`. 78 tests before and after
    (4 + 15 + 21 + 6 + 32), and the five migrated files keep their counts
    (`CalibrationPage` 9, `EcuBackupCard` 27, `RomLibraryPage` 28,
    `RomEditorPage` 44, `RomEditorTables` 11, `TuningContextBar` 12).

- [x] **R28.5 `SimForm` and `RomEditorPage.Workspace` mix state machines,
  request orchestration and layout.** `SimForm` is one 712-line component
  (five state cells, three effects, about 450 lines of JSX); `Workspace` is
  504 lines. Four hand-rolled request-generation counters (`request`,
  `counter`, `simRun`, `load`) are one idea. Done 2026-11-15.
  - [x] `SimForm`: `useSimFormState(tables, label, edits)` for the values,
    remembered set, presets and typical-value fill; the typical-values fetch
    onto the existing async hooks; the derived read-out (`mode`, `sized`,
    `cost`, `sizeVerdict`, …) into `lib/romSimView` / `lib/romSimSize`;
    sections as components (`SimPresets`, `SimEdges`,
    `SimRememberedBanner`).
    `lib/romSimForm.ts` is the form's reducer (`initSimForm`, `simFormReducer`:
    change, preset, resetEdges, clearRemembered, typicalLoading / Failed /
    Filled; values, remembered set, saved-at, typed edges, applied preset,
    typical marks and typical state in one state, the remember rules in the
    one `change` action; storage stays with the caller). `hooks/useSimFormState`
    wraps it with `loadRemembered` / `forgetRemembered`, the preset fills, and
    the typical fetch. `simFormReadout` (in `lib/romSimSize`, which already
    held the cost model and imports `romSim`; `romSimView` is the result
    view's) is the whole derived read-out: check, missing / invalid, grid size
    text, `loadOutside`, `drawsStated`, `mode`, `sized`, `cost`, `sizeVerdict`,
    `wotWarning`. `SimForm.tsx` 1,048 to 351 lines; `SimFields.tsx` (the field
    primitives), `SimFormSections.tsx` (`SimRememberedBanner`, `SimPresets`,
    `SimTypicalValues`, `SimEdges`) and `SimRunStatus.tsx` (`SimRunning`,
    `SimCancelled`, `SimFailureAlert`, moved with their text unchanged).
    Deviation: the typical-values fetch is not on `usePolling` /
    `useAsyncAction`: the first re-fetches by effect and clears on disable, the
    second has no abort or cache and a notice-shaped error, and the click is an
    event whose answer is applied to the form as it is when the values
    arrive. It is on `useLatestRequest` (below), the same token and abort the
    simulate run uses, which is what made it one idea.
  - [x] `Workspace`: `useEditSession(sha)` (pending edits and persistence),
    `usePreviewExport`, `useSimRun`; the component keeps layout.
    `hooks/useEditSession(sha, onEdited)` (pending, label, add / remove /
    setLabel, storage; every change calls `onEdited`, which is the preview
    hook's `invalidate`, so a mutation that forgets to drop the preview cannot
    be written), `hooks/usePreviewExport(token, sha)` (preview, export,
    download, one generation; the label and edits are arguments, so the hooks
    have no cycle), `hooks/useSimRun` (sim state, timings, abandoned run,
    `stale`, run / cancel). `Workspace` is layout plus the table choice,
    selection and `apply`. The four counters are `hooks/useGeneration`:
    `useGeneration()` (`next` / `bump` / `isCurrent` / `current`: `request`,
    `load`, and the edit-id `counter`) and `useLatestRequest()` (a generation
    plus its `AbortController`, cancelled on unmount: `simRun` + `simAbort` and
    the typical fetch's controller). A pending edit's id is now claimed before
    the state update, not inside it (two adds in one batch could have read the
    same counter).
  - Must not change: `SIM_REQUEST_FIELDS`, the remember rules, `data-*` and
    aria names. Pinned by `RomEditorSimulate*.test.tsx` (split by R28.4),
    `RomEditorPage.test.tsx`, `SimResults.test.tsx`, `lib/romSim*.test.ts`,
    `e2e/romEditor.spec.ts`.
    Held: none of those files was edited (the 374 tests of the RomEditor,
    romEditor and romSim files passed before and after; `PreviewResult.tsx`
    re-exports `ExportState`, now defined in `usePreviewExport`, so its test's
    import is unchanged); `e2e/romEditor.spec.ts` 11 passed. New unit tests:
    `lib/romSimForm.test.ts` (16), `simFormReadout` in `romSimSize.test.ts`
    (+5), `hooks/useGeneration.test.ts` (5), `useEditSession` (4),
    `usePreviewExport` (6), `useSimRun` (7), `useSimFormState` (7).
    Mutation-checked, each restored byte-identical: `change` not dropping the
    remembered mark fails `takes the remembered mark off the edited field only`;
    `isCurrent` always true fails 9 tests across `useGeneration`,
    `usePreviewExport` and `useSimRun` (the stale-answer and Cancel ones);
    `invalidate` not bumping the generation fails the two
    `usePreviewExport` drop-after-edit tests; `mode` ignoring `unchanged`
    fails the preset-then-edit readout test. Gate: `pnpm typecheck`, `pnpm
    lint` (no suppression change), `pnpm format:check` clean; `pnpm test` 152
    files / 1563 tests passed.

- [~] **R28.6 One 782 KB chunk (232 KB gzip), loaded eagerly.**
  `vite.config.ts` says to revisit the single chunk "past a few hundred KB".
  There is no `React.lazy` or dynamic `import()`. React is the only runtime
  dependency, so it is all app code; `panels/tuning/**` and `lib/rom*` are
  about a third of it and are used only after opening Engine Tuning, while the
  in-vehicle landing screen is `DriveScreen`.
  - [~] Measure first load and parse time on a phone over the Pi's Wi-Fi.
    Sizes measured offline 2026-11-15 (`pnpm build`); the on-phone load and
    parse time still needs the truck/Pi and a phone, not run.
  - [x] `React.lazy` for the tuning group and the Workbench. Done 2026-11-15.
    `lib/lazyChunk.ts` (`lazyChunk(() => import(..), "Name", {memoised})`
    wraps `React.lazy`; a failed fetch becomes a `ChunkLoadError`).
    `App.tsx` loads the whole `Workbench` lazily (the Drive screen is the
    landing page), and `Workbench.tsx` loads `LoggingPage`, `LogsPage`,
    `AnalysisPage`, `RomLibraryPage`, `CalibrationPage` and `RomEditorPage`
    lazily, each inside the existing `PanelErrorBoundary` plus a `Suspense`
    whose fallback is the new `components/PageLoading` (a quiet
    "Loading…" status line). Idle-time prefetch after `load` (production
    only) of the Workbench chunk and then, one at a time, every Engine Tuning
    chunk (`Workbench.prefetchTuningPages`, operator 2026-11-15) so the
    service worker has them all cached; a failed fetch is skipped and that
    page loads on first visit instead.
    Measured, vite build raw / gzip (vite's kB = 1000 B):
    before, one chunk `index` 789.37 / 235.37 kB plus css 66.55 / 11.71 kB;
    after, the default (Drive) route loads `index` 228.02 / 72.81 kB plus the
    same css (first-load JS gzip 235 -> 73 kB, -69 %). Lazy chunks:
    `Workbench` 288.13 / 81.01, `RomEditorPage` 155.36 / 49.46,
    `RomLibraryPage` 37.32 / 11.47, `AnalysisPage` 26.25 / 8.77,
    `cellOverlay` 22.01 / 8.47 (shared by the ROM pages), `LoggingPage`
    13.13 / 4.58, `LogsPage` 12.33 / 4.65, `CalibrationPage` 6.55 / 2.52,
    `csv` 6.03 / 2.48, `workbench` 3.45 / 1.17, `workbenchApi` 1.63 / 0.84.
    `index.html` modulepreloads nothing else. Gates: `pnpm typecheck`,
    `format:check`, `lint` (no new suppressions), `pnpm test` (153 files),
    `pnpm build`, `pnpm e2e` (47 passed, 3 skipped, incl. `offlineChunks.spec.ts`) all green.
  - [x] A failed chunk load is recoverable, not a blank page. Done 2026-11-15.
    `PanelErrorBoundary` shows a `ChunkLoadError` ("A part of the dashboard
    could not be loaded ..., probably updated since this tab was opened")
    with a Reload button instead of "Try again" (React caches a rejected
    lazy load, so a retry in place can never succeed). The Workbench chunk
    itself sits in its own `PanelErrorBoundary`. Tests:
    `lib/lazyChunk.test.tsx` (loading line, memoised once, failed fetch ->
    alert + Reload calls `location.reload`).
  - [x] A size budget so it stays fixed. Done 2026-11-15. `vite.config.ts`
    plugin `entryBudget` fails `pnpm build` when the entry chunk plus its
    static imports exceed `ENTRY_GZIP_BUDGET_BYTES` (90,000 B gzip; now
    72.8 kB); checked by temporarily setting 50,000 and watching the build
    fail. `layering.test.ts` "reaches the Workbench and the Engine Tuning
    pages only through import()" refuses a static import of the Workbench
    from `App.tsx` or of a tuning page from `Workbench.tsx`.
  - [x] `public/sw.js` still serves the lazy chunks offline. Done 2026-11-15.
    `sw.js` never precached hashed assets (its shell list is the HTML, manifest
    and icons); hashed JS/CSS are cache-first on first fetch and `sw.js` is
    unchanged. `e2e/offlineChunks.spec.ts` runs the production bundle
    (`vite preview`) with the real worker registered: it waits for the worker
    to control the page and for the idle prefetch to land the Workbench,
    `LoggingPage`, `RomEditorPage` and `CalibrationPage` chunks in the Cache
    Storage, then `context.setOffline(true)`. Offline, the Workbench opens and
    an Engine Tuning page never opened online (`#wb/tuning-calibration`)
    renders, both served from the worker's cache. Passed (chromium); it fails
    with the Workbench prefetch removed and, separately, with the tuning
    prefetch removed. Operator decision 2026-11-15: prefetch the tuning chunks
    too (~90 kB gzip of background Wi-Fi after the Drive screen is up; the
    entry budget is unaffected). A chunk that is genuinely missing (a stale
    tab after a Pi update) still ends in the Reload notice:
    `lib/lazyChunk.test.tsx` and
    `tests/test_dashboard_api.py::test_a_vanished_asset_is_a_404_not_the_index`.

- [x] **R28.7 Formatters and range math outside `util/` and `lib/`.**
  - Two exported `formatDuration` with different output (`util/time.ts`,
    `lib/romSimSize.ts`); `ModelEvidencePanel` shadows `formatNumber`; `fmt`
    twice; nine sites format a timestamp four different ways.
  - `Math.min(...values)` / `Math.max(...values)` 12 times across five grid
    renderers, and "`hi − lo || 1`" spelled out eight times, while
    `lib/framePlot.ts` already has `extent()` and `fraction()`.
    `ChartsPanel.tsx` spreads a per-channel history into `Math.max`, which
    throws past roughly 100k values.
  - [x] `formatDateTime` in `util/time`; rename the ROM one `formatRunTime`;
    `formatMs` / `formatVolts` into `util/format`. Done 2026-11-15.
    `formatDateTime(value, {unit, style, missing})` replaces the bare
    `toLocaleString()` copies (`FrameNotes`, `CalibrationPage`,
    `RomLibraryPage` x2), `LogBrowser`, `CompanionConfigCard.formatRanAt` and
    the body of `pairedDevices.formatWhen` (now a one-line named call);
    `MultiSeriesChart.formatTimeLabel` was `formatTimeOfDay` verbatim and is
    deleted; `AlarmsPanel`, `FrameTable` and `WifiNetworksCard` use
    `formatTimestamp` (now also takes epoch ms). `romSimSize.formatDuration` is
    `formatRunTime`. `formatMs(value, missing)` / `formatVolts` moved to
    `util/format` (`ModelHealthPanel` passes `"idle"`); `formatNumberOrDash`
    is `formatReading`; the `ModelEvidencePanel` `formatNumber` shadow is
    `formatNumber(x, EVIDENCE_DIGITS)`; both `fmt` copies are
    `formatReading` with a named options constant. Output differences found
    are recorded in the dispatch report: a bare copy printed "Invalid Date"
    for an unparseable stamp (now the raw text), `formatTimestamp`'s fallback
    sat in a `catch` that never ran (now works), `formatMs` printed "NaN ms",
    `formatDuration({collapseZero})` printed "NaNs". Tests: `time.test.ts`
    (`formatDateTime`, `formatTimestamp`, both `formatDuration` variants),
    `format.test.ts` (`formatMs`, `formatVolts`), `romSimSize.test.ts`
    (`formatRunTime`).
  - [x] Route the renderers through `extent` / `fraction`; one
    `coverageMax(grid)`. Done 2026-11-15. `lib/framePlot` gains `rangeSpan`
    (the one "hi - lo || 1") and `coverageMax`; `Heatmap`, `Sparkline`,
    `Curve`, `MultiSeriesChart` (its own `seriesExtent` deleted), `MapTable`,
    `CellGrid`, `RomTablesView`, `WorkbenchNotices`, `DynoView`,
    `ModelEvidencePanel.OverlayChart`, `TrainingPanel` and `ChartsPanel`
    (the per-channel history spread) use them; `MapTable` and `CellGrid` use
    `coverageMax`. `framePlot.test.ts` covers `rangeSpan`, `coverageMax` and a
    500k-value `extent`. `layering.test.ts` now fails on a shared formatter
    defined outside `util/`, a `Math.min/max(...x.flat())` and a hand-written
    "hi - lo || 1". Left as bounded spreads (a handful of channels or
    cylinders, never a history): `statusDemand`, `PairingApprovalPrompt`,
    `MonitorsPanel`, `ExplorePanel`, `ModelHealthPanel`, `TrendsPanel`,
    `evidence.maxOf`. Gate: `pnpm typecheck`, `pnpm format:check`,
    `pnpm lint` clean, no new suppressions, `pnpm test` 152 files / 1588
    passed.
  - The renderers themselves stay separate (R08, "looked at and fine").

- [ ] **R28.8 The type scale is ad hoc.** 392 arbitrary `text-[0.NNrem]`
  classes in 25 sizes; the same eyebrow-label string appears nine times and a
  near-twin seven. `styles.ts` already holds shared class tokens.
  - [ ] `eyebrowClass` plus four or five `--text-*` tokens in `index.css`
    `@theme`; sweep the repeated strings; a `themeClasses.test.ts`-style check
    that fails on a new arbitrary rem size.

- [ ] **R28.9 Small items.**
  - `postFeedback` takes seven positional arguments and `AnomalyPanel` casts
    its event `as unknown as Record<string, unknown>` to call it. Take an
    `AnomalyEvent` and an options object.
  - `lib/romSimSize.ts` `forgetTimings` has no callers.
  - `api/http.ts` is 1,087 lines; five domains are already split out. Split
    captures, jobs, network and tuning the same way when next touched.
  - `AGENTS.md` still says ESLint's pre-existing violations "are
    bulk-suppressed in `eslint-suppressions.json`". The file is empty; say so,
    and keep the `pnpm lint:prune` rule only if the mechanism is still wired.

## Looked at and fine

`lib/canvas.ts` `prepareCanvas` serves all four canvas charts; color ramps
live only in `lib/colorScale.ts`; `FrameTable` windows its rows and the anomaly
list is capped; `useAsyncAction` (24 users) and `useControlResource` (53) are
well adopted; fixtures generated from real Python payloads
(`romLevers.fixture.json`) are the pattern R28.2 extends; `SessionSummary`,
`JobRecord`, `PowerSnapshot`, preflight and version types match Python.

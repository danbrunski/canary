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

- [ ] **R28.3 Hand-rolled `setInterval` polling has regrown beside
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
  - [ ] Move the two panel reads to `useControlResource(fetcher, {intervalMs})`,
    keeping "first read reports, re-reads stay quiet".
  - [ ] `RescoreControl` becomes a `usePolling` enabled while the job runs.
  - [ ] Replace the six clocks and `SimForm`'s copy with `useNow`.
  - [ ] Make it stay fixed: a lint rule or a source test that fails on
    `setInterval` outside `hooks/` (`useTagQueue`'s flush is a queue, not a
    poll; allow it by name).

- [ ] **R28.4 A shared control-route test harness, before R28.5.** The same
  `route()` mock keyed on `` `${method} ${path}` `` is hand-written in six
  test files (`RomEditorSimulate`, `RomEditorPage`, `RomLibraryPage`,
  `CalibrationPage`, `EcuBackupCard`, `PairedDevicesPanel`), with variants in
  three more; 19 files `vi.mock("…/api/core")`; `cell()`, `bodyOf()`,
  `renderEditor()` and one `afterEach` are each copied two to four times.
  `RomEditorSimulate.test.tsx` is 1,878 lines and 73 tests.
  - [ ] `src/test/controlRoutes.ts`: `routeControl(table, {fallback})`,
    `bodyOf`, `callsTo(path)`, `click`, `type`, `renderRomEditor(sha)`,
    following `test/harness.ts` `mockHttp`.
  - [ ] Split `RomEditorSimulate.test.tsx` along its existing `describe`
    blocks (presets and cost, cancel, typical values, timing).

- [ ] **R28.5 `SimForm` and `RomEditorPage.Workspace` mix state machines,
  request orchestration and layout.** `SimForm` is one 712-line component
  (five state cells, three effects, about 450 lines of JSX); `Workspace` is
  504 lines. Four hand-rolled request-generation counters (`request`,
  `counter`, `simRun`, `load`) are one idea.
  - [ ] `SimForm`: `useSimFormState(tables, label, edits)` for the values,
    remembered set, presets and typical-value fill; the typical-values fetch
    onto the existing async hooks; the derived read-out (`mode`, `sized`,
    `cost`, `sizeVerdict`, …) into `lib/romSimView` / `lib/romSimSize`;
    sections as components (`SimPresets`, `SimEdges`,
    `SimRememberedBanner`).
  - [ ] `Workspace`: `useEditSession(sha)` (pending edits and persistence),
    `usePreviewExport`, `useSimRun`; the component keeps layout.
  - Must not change: `SIM_REQUEST_FIELDS`, the remember rules, `data-*` and
    aria names. Pinned by `RomEditorSimulate.test.tsx`,
    `RomEditorPage.test.tsx`, `SimResults.test.tsx`, `lib/romSim*.test.ts`,
    `e2e/romEditor.spec.ts`.

- [ ] **R28.6 One 782 KB chunk (232 KB gzip), loaded eagerly.**
  `vite.config.ts` says to revisit the single chunk "past a few hundred KB".
  There is no `React.lazy` or dynamic `import()`. React is the only runtime
  dependency, so it is all app code; `panels/tuning/**` and `lib/rom*` are
  about a third of it and are used only after opening Engine Tuning, while the
  in-vehicle landing screen is `DriveScreen`.
  - [ ] Measure first load and parse time on a phone over the Pi's Wi-Fi.
  - [ ] If it matters: `React.lazy` at `Workbench.tsx`'s `renderItem` switch
    for the tuning group, inside the existing `PanelErrorBoundary`. Check
    `public/sw.js` still precaches every chunk (the dashboard must work with
    no network beyond the Pi) and `e2e/phone.spec.ts`.

- [ ] **R28.7 Formatters and range math outside `util/` and `lib/`.**
  - Two exported `formatDuration` with different output (`util/time.ts`,
    `lib/romSimSize.ts`); `ModelEvidencePanel` shadows `formatNumber`; `fmt`
    twice; nine sites format a timestamp four different ways.
  - `Math.min(...values)` / `Math.max(...values)` 12 times across five grid
    renderers, and "`hi − lo || 1`" spelled out eight times, while
    `lib/framePlot.ts` already has `extent()` and `fraction()`.
    `ChartsPanel.tsx` spreads a per-channel history into `Math.max`, which
    throws past roughly 100k values.
  - [ ] `formatDateTime` in `util/time`; rename the ROM one `formatRunTime`;
    `formatMs` / `formatVolts` into `util/format`.
  - [ ] Route the renderers through `extent` / `fraction`; one
    `coverageMax(grid)`.
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

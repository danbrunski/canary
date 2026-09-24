# R08 — dashboard frontend

Opened 2026-09-24 from the refactor review ([R00](R00-refactor-README.md)).
The stack is React 18, TypeScript, Vite and Tailwind v4, and `pnpm typecheck`
is clean.

The July refactor gave the dashboard canonical homes for helpers:
`util/format.ts`, `util/time.ts`, `util/storage.ts`, `useControlResource`,
`useAsyncAction`, `ResourceGate` and `Notice`. Nearly all the new debt is in
code written *after* it: the Engine Tuning pages (`panels/tuning/*`),
`RomMapsPanel`, `lib/workbenchApi.ts` and `CaptureProfilesPanel`. That code
re-rolled fetching, error text, formatting and colours instead of using those
homes.

Line counts understate the new code. `RomMapsPanel` is only 369 lines, but it
is 27.6k characters and 48 of its lines are over 140 characters. By
characters, `CaptureProfilesPanel` is 43.6k and `LogsPage` 24.0k.

## Rules

- **Gate:** `pnpm typecheck` plus the vitest files touched. Re-run
  `TuningFeedback.test.ts` once on a flake.
- **Test first where nothing tests the code.** These components and hooks
  have no behavioural test: `CalibrationPage`, `LoggingPage` (a smoke render
  only), `TestSessionPanel`/`TestSessionScreens`, `ControlPanel` and its
  cards, `WorkbenchNotices`, `Curve`, `Heatmap`, `Sparkline`,
  `MultiSeriesChart`, `useStatus`, `useBehavior*`, `useAsyncAction`,
  `useTagQueue`, and `useControlResource`'s polling path. An item that
  changes one of them adds a test before the change.
- **The July calls stand.** `RankedBars` and `ChannelBars` stay separate.
  `api/http.ts` stays one function per route (R08.3 extends that rule).
- **No new tools without asking.** There is no TS formatter or linter config,
  which would explain the 140+ character lines. Adding prettier or eslint is
  a tool install, so ask the operator (see the memory note on installing
  tools).

---

## Correctness first

- [ ] **R08.1 Fix off-theme classes on the tuning pages.** *Visual bug.*
  - `text-fg-muted` is used 17 times: 14 in `RomMapsPanel`, 3 in
    `CalibrationPage`.
  - `index.css`'s `@theme` defines `--color-fg`, `--color-fg-dim` and
    `--color-fg-faint`, but no `--color-fg-muted` (verified 2026-09-24).
    Tailwind v4 therefore generates nothing for the class, and those "muted"
    notes render at full foreground colour.
  - Raw palette classes appear 8 times (`text-red-400`, `text-amber-300/400`,
    `text-emerald-400`, `text-sky-300`, `text-slate-950`) where the tokens
    `text-bad`, `text-warn`, `text-ok` and `text-fg-dim` should be used. They
    ignore the night theme.

  **Do:** map each class to its token. **Accept:** a vitest that fails if any
  source file uses `text-fg-muted` or a raw `-(red|amber|emerald|sky|slate)-\d{3}`
  colour class. Size **S**.

- [ ] **R08.2 Put the Engine Tuning pages on the shared error vocabulary and
  resource hooks.** *User-visible bugs.*
  - **Error text.** Six sites build the message as `err instanceof Error ?
    err.message : String(err)` instead of calling `describeError`:
    `CalibrationPage` ×2 (plus `String(reason)` in its refresh),
    `RomMapsPanel`, `CaptureProfilesPanel` and `TrainingPanel`.
    `describeRefusal` in `workbenchApi` falls back to `error.message` for
    anything unstructured. So on every tuning page a dead Pi reads "Network
    request to /control/logs failed", not the "Can't reach the daemon…" text
    that `api/core.ts` says must be loud, and a 401 shows `HTTP 401:
    {"detail":…}`.
  - **Hand-rolled fetching.** Eight sites manage their own
    fetch/loading/error state: `LogsPage` (LogBrowser, LogViewer),
    `AnalysisPage` (SessionChooser, TimingView revisions), `CalibrationPage`
    ×2, `RomMapsPanel` (profiles, residency). Bugs found in them:
    - `SessionChooser` has no `AbortController` and never clears `refusal`
      after a success, so toggling "Include ordinary sessions" can race.
    - `TimingView` shares one `refusal`/`busy` between Cells and Compare, so
      a Compare refusal shows under Cells.
    - `AnalysisPage` has three copies of the `[result, refusal, busy]` trio
      behind a module-level `run()`.
  - `/control/logs` is fetched by two components with the same checkbox.
  - Why these pages didn't use `useControlResource`: it always polls, and it
    flattens errors to strings.

  **Do:**
  - `describeRefusal` falls back to `describeError`.
  - `useControlResource` gains `intervalMs: null` (fetch once) and a
    `describe?: (err) => E` mapper, so the workbench keeps its structured
    `Refusal`.
  - Add `useLogs(token, includeSessions)` and `useAnalysisRun<T>()`, which
    returns result/refusal/busy/run, with one per Timing mode.
  - `CalibrationPage` moves to `useAsyncAction` + `Notice`.

  **Accept:**
  - Hook tests first: `useControlResource.test.ts` today only covers
    `describeError`.
  - Failing tests for the SessionChooser race and the shared TimingView
    refusal.
  - `AnalysisPage.test.tsx` (7), `LogsPage.test.tsx` (13) and
    `RomMapsPanel.test.tsx` (20) pass. They mock `api/core.controlJson` by
    path, so they are robust to this.

  Size **M**.

- [ ] **R08.3 Route every localStorage access through `util/storage`.**
  *Crash bug.*
  - `TestSessionPanel` calls `window.localStorage.getItem` in a `useState`
    initializer and `setItem` in an effect, both without try/catch (verified
    2026-09-24). Where storage is blocked this throws, and the panel crashes,
    mid-drive if that is when it happens.
  - `TelemetryPanel.loadStoredKeys` and `useTagQueue.loadQueue`/`saveQueue`
    hand-roll `readJson`/`writeJson`.

  **Do:** `readString`/`writeString` in `TestSessionPanel`; `readJson` plus
  validation in the other two. **Accept:** a test that renders
  `TestSessionPanel` with a throwing `localStorage`. `TelemetryPanel.test`
  (18) passes. Size **S**.

## Structure

- [ ] **R08.4 One function per route: move API calls out of panels and
  `lib/`.**
  - Routes live in three layers:
    - `api/http.ts`: 72 exports.
    - `lib/workbenchApi.ts`: 6 routes, ~200 lines of response types, and the
      non-API helpers `toCsv`, `formatDuration` and `gateSentence`.
    - Inline `controlJson` calls in panels: `CalibrationPage` ×4,
      `RomMapsPanel` ×3, `AnalysisPage` ×1.
  - Duplicates:
    - `RomMapsPanel` re-implements `/control/capture/profiles`, although
      `getCaptureProfiles` exists.
    - `/control/calibration` is typed twice: `CalibrationPage`'s
      `CalibrationData`, and `AnalysisPage`'s inline
      `{history: Array<{revision}>}`.
  - `CalibrationPage` sets `Content-Type` by hand, which `controlJson`
    already does.
  - The workbench response types are split across `lib/workbenchApi.ts`,
    `lib/romMaps.ts`, `lib/romTrace.ts` and `CalibrationPage`.

  **Do:**
  - Create `api/workbench.ts` with one function per route: getLogs,
    getFrame, the four analyses, getCalibration, recordFlash,
    getMemoryChannels, verifyMemoryChannels, inspectRom and
    getCellResidency.
  - Create `types/workbench.ts`, re-exported from the barrel.
  - `workbenchApi.query()` becomes `api/core.queryString()`, also used by
    `getBinnedAnalysis` and `getSessionChart`.
  - `toCsv` moves to `util/`, and `formatDuration` is handled in R08.5.
    `describeRefusal`/`gateSentence` stay in `lib/`.

  **Accept:** no `controlJson` call outside `api/`. The `workbench helpers`
  tests in `LogsPage.test` update their imports. Size **M**.

- [ ] **R08.5 Sweep the post-July formatter re-rolls into `util/`.**
  - `lib/workbenchApi.formatDuration` has the same exported name as
    `util/time.formatDuration` but formats differently (`h:mm:ss`/`m:ss` vs
    `Ym Zs`). That is an auto-import hazard.
  - `TuningContextBar.elapsedLabel` is a third h:mm:ss variant, sitting
    beside `util/time.formatElapsed`.
  - `DataSourcesPanel.GpsTab` re-declares `KM_PER_MILE`/`M_PER_FT` and
    hand-copies `formatAltitude`/`formatGpsSpeed`. Its heading drops the
    cardinal that `formatHeading` adds. The July sweep missed it; it dates
    from May.
  - `TripPanel` re-declares `KM_PER_MILE`, and its `channelValue` is just
    `numberOrNull(...)`.
  - There are five live-value formatters, all different (1234.5 renders as
    "1235", "1234.5" or "1234.50"): `LogsPage.formatValue`,
    `DataSourcesPanel.trimNum`, `ChartsPanel.fmt`, `behaviorOverlay.fmt` and
    `CaptureProfilesPanel`'s `toFixed(2)`.
  - Nullable-with-unit one-offs: `PowerPanel.formatPct`/`formatVolts` and
    `ModelHealthPanel.formatMs`/`formatNumberOrDash`. The missing-value
    placeholder varies: "-", "—", "" and "unknown".
  - Stream keys are hand-built: `obd:mem:${name}` appears in
    `CaptureProfilesPanel` and twice in `DataSourcesPanel`, though
    `lib/romMaps.streamRow` already encodes the rule.

  **Do:**
  - `util/time.formatClockDuration(seconds)` replaces both clock-style
    formatters.
  - `GpsTab` uses the `util/format` GPS helpers, with the unit constants
    exported once.
  - Add `util/format.formatReading(value, {digits, trimZeros, bigAt})`,
    where each caller keeps its current output through options.
  - Add `streamKey(spec)` beside `streamRow`, and make it the only way those
    keys are built.

  **Accept:** `LogsPage.test` ("formats durations"), `TuningContextBar.test`,
  `format.test.ts`, `DataSourcesPanel.test` (8) and `TripPanel.test` (9)
  pass. The GPS heading cardinal is a named behaviour change. Size **M**.

- [ ] **R08.6 Split `CaptureProfilesPanel` (840 lines) into runner and
  editor.**
  - The panel does two jobs, switched by flags, and production renders
    exactly one half each: Workbench uses `authoring showRunner={false}`,
    LoggingPage uses `authoring={false} showRunner`.
  - **All 27 tests in `CaptureProfilesPanel.test.ts` render the default
    (both true), a mode no caller ships.** Neither production mode is tested
    directly.
  - Possible drift: the capture table's "requested" column shows
    `configured_rate_hz` for DIDs, while `TelemetryPanel.effectiveTargetHz`
    uses `1 / window_interval_seconds`. This is the frontend twin of R02.3.

  **Do:**
  - `useCaptureCatalog(token)`: profiles, resolved plans, pids, dids and
    memory.
  - `CaptureRunner.tsx`, with a pure `capturePhase(busy, active)`, holding
    the optimistic state, phase machine, Space-key pause, live values and
    scheduler table.
  - `CaptureProfileEditor.tsx`: draft, toggles, `ChannelPicker`, save and
    delete.
  - `lib/scheduler.ts` exports `matchingRequest` and `effectiveTargetHz`,
    used by both panels.

  **Accept:** the tests are re-targeted at the two shipped modes, and
  `LoggingPage` gains a behavioural test. **Operator decision:** which DID
  rate is the "requested" rate? Settle it before unifying, and after R02.3
  so the backend preview it displays is right. Size **L**.

- [ ] **R08.7 One chart palette that follows the theme.**
  - There are 30+ hex literals across 12 files for canvas and SVG. Some are
    near-duplicates of tokens: `#FFB84A` vs accent `#ffb547`, `#ef4444` vs bad
    `#ff6f6f`, and `#94a3ad` (5 uses) is not a token at all.
  - A `Heatmap.tsx` comment calls `#1f262d` "bg-elev-2", but the token is
    `#1b232c`.
  - There are two 6-colour series palettes: `ChartsPanel` vs `LogsPage`.
  - There are five unrelated heat ramps: `Heatmap.ramp`,
    `WorkbenchNotices.ramp`, `romMaps.mapColor`, `romTrace.coverageColor`,
    and LogsPage's HSL rainbow (×2). The trim surface and the ROM map are
    adjacent tuning pages coloured differently.
  - `[data-theme="night"]` recolours the tokens but none of the canvas or
    SVG strokes.

  **Do:**
  - `lib/chartTheme.ts`: `cssColor("--color-accent")` through
    `getComputedStyle` for canvas; Tailwind `stroke-*`/`fill-*` classes for
    SVG; one `SERIES_PALETTE`.
  - `lib/colorScale.ts`: named `sequential`, `diverging` (for deltas) and
    `coverage` ramps.

  **Accept:** screenshots of the chart pages in both themes before and
  after. jsdom has no canvas, so the check is visual; use the `run` skill or
  Playwright. Size **M**.

- [ ] **R08.8 Split `RomMapsPanel`.**
  - The 7-branch `gateReason`/`traceReason` chain is inline.
  - The coverage overlay is a state machine spread over four keyed `Record`
    states: `coverageOn`, `coverageSource`, `residency` and `liveCoverage`.
    `fetchResidency`, `coverageFor` and a refetch effect belong to it.
  - `MapTable` sits in the same file.

  **Do:** a pure `traceGate(...)` in `lib/romTrace.ts` with unit tests, a
  `useCoverageOverlay` hook, and `MapTable.tsx` in its own file. **Accept:**
  the 20 vitest tests and `e2e/romMaps.spec.ts` pass. Size **M**.

- [ ] **R08.9 One poll loop.**
  - `useStatus`, `useBehaviorModels` and `useBehaviorLive` are three
    near-identical `setTimeout`-after-completion loops.
  - `useControlResource` uses `setInterval`, which fires again while the
    previous request is still in flight. The worst case is `LoggingPage`
    polling `/control/captures/current` every 2.5 s during captures, when
    the Pi is busiest.
  - `ModelHealthPanel` shows `statusError.message` raw, not `describeError`.

  **Do:** a `usePolling(fetcher, {intervalMs, enabled})` core with
  completion-chained scheduling. `useControlResource` and the three hooks
  are built on it. **Accept:** hook tests first. With fake timers, a slow
  fetch never overlaps the next one. Size **M**.

- [ ] **R08.10 Shared file-download helper.**
  - Five copies of the Blob → object URL → anchor click → revoke sequence:
    `ChartsPanel.downloadBlob(blob)`, `MultiSeriesChart.downloadBlob(content,
    mime)`, `LogsPage.downloadText`, and inline in `SessionsCard` and
    `CustomDashboardPanel`.
  - `api/http.downloadSession` and `downloadSessionTrack` are two copies of
    an authorized blob fetch. `downloadSession` lacks its sibling's
    `baseUrl`.

  **Do:** add `util/browser.downloadBlob`/`downloadText` and
  `api/core.controlBlob(path, token, init, baseUrl)`; the latter is July's
  optional item. **Accept:** `ExportPanel.test` and
  `ChartsPanelSessionMode.test` pass. Size **S**.

- [ ] **R08.11 Delete dead code and add one segmented-control component.**
  - Never referenced anywhere, in `src` or `e2e`: `styles.ts`
    `groupTabClass`, `groupTabsClass` and `dimTextClass`; `nav.ts`
    `groupOf`; `api/http.ts` `getConfig` and `getLatestTelemetry`.
  - `AnalysisPage` (×2) and `LogsPage` (×1) re-roll segmented toggles with
    `cn(buttonClass, active && "border-accent text-accent")`. Their
    accessibility differs: `role="tab"`/`aria-selected` in one place,
    `aria-pressed` in another.

  **Do:** add `components/SegmentedControl.tsx` with one accessibility
  pattern, reusing or deleting the orphaned `groupTab*` styles. Delete the
  rest. **Accept:** the tuning tests that query these buttons by role pass,
  updated if the role changes. Size **S**.

- [ ] **R08.12 Low-value mechanical sweeps (batch them, or skip them).**
  - Ten files hand-write the `panelClass` + `<header>` + `<h2>` shell instead
    of using `<Panel title count actions className>`: ControlPanel,
    ControlPanelSections, JobsCard, SessionsCard, ReplayCard,
    ModelUploadCard, SequenceDetectorCard, TestSessionScreens,
    BehaviorModelsPanel and CaptureProfilesPanel.
  - The 8-line `devicePixelRatio` canvas setup is repeated in `Sparkline`,
    `Curve`, `Heatmap` and `MultiSeriesChart`. Add `prepareCanvas(canvas, w,
    h)`.

  Size **S–M**.

## Looked at and fine

- `api/http.ts` length: still one function per route. Only the workbench
  spill-over changed (R08.4).
- `types/`: no duplicate exported names.
- `CellGrid` vs `MapTable` vs `Heatmap`: different semantics. Share the ramps
  (R08.7), not the renderers.
- ChartsPanel's "session mode" overlaps Logs → Visual, and ExplorePanel
  overlaps the Logs heat mode. Retiring either is a product decision, not a
  refactor.
- Generating TS types from Python: the routes return dicts with no
  `response_model`s, so there is nothing to generate from.
- `DataSourcesPanel` tabs already use `ResourceGate`.
- `TelemetryPanel` and `BehaviorModelsPanel`: large but cohesive.
- The Workbench `renderItem` switch: fine.
- `useLocalStorageJson` adoption is good in the new code (LogsPage,
  useTuningContext, StatusStrip, AlarmsPanel, CustomDashboardPanel).

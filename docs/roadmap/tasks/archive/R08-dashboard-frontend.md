# R08 — dashboard frontend

Opened 2026-09-24 from the refactor review ([R00](../R00-refactor-README.md)).
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
- **Lint and format (R08.13, operator 2026-09-24).** `pnpm format` (Prettier)
  and `pnpm lint` (type-aware ESLint with a bulk-suppressed baseline) are part
  of the gate. An item that fixes a baselined violation runs
  `pnpm lint:prune` in the same commit.

---

## Correctness first

- [x] **R08.1 Fix off-theme classes on the tuning pages.** *Visual bug.*
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

  **Done 2026-09-25.** `text-fg-muted` → `text-fg-dim` (14 in RomMapsPanel, 3
  in CalibrationPage); `text-red-400` → `text-bad`, `text-amber-300/400` →
  `text-warn`, `text-emerald-400` → `text-ok`, `text-sky-300` →
  `text-accent-2` (a distinct cyan, as before), `text-slate-950` (dark text on
  ROM heat cells) → `text-bg`. One more undefined class had the same shape:
  `border-danger/60 text-danger` on the infeasible-plan box, now `bad`.
  `src/themeClasses.test.ts` fails on any raw
  `-(red|amber|emerald|sky|slate|…)-\d{2,3}` class and on any semantic colour
  class (`fg*`, `elev*`, `accent*`, `ok|warn|bad|danger|muted|…`) whose token
  `index.css` does not define. Verified to fail when `text-fg-muted
  text-red-400` is put back.

- [x] **R08.2 Put the Engine Tuning pages on the shared error vocabulary and
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

  **Done 2026-09-25.**
  - `describeRefusal` falls back to `describeError` for anything unstructured
    (and for a structured detail with no message).
  - `useControlResource` is built on `usePolling` (R08.9) and takes
    `intervalMs: null` (fetch once, again on `refresh`) and `describe` (the
    workbench passes `describeRefusal`). It also returns `stale`: the fetcher
    changed and its first fetch has not settled.
  - `hooks/useWorkbench.ts`: `useLogs(token, includeSessions)` (LogBrowser and
    SessionChooser) and `useAnalysisRun<T>()`; Delivery, Trim, Timing-cells and
    Timing-compare each own one. `LogViewer` reads frames through
    `useControlResource`, with the page offset derived from the view key (no
    reset effect, so the stale offset is never requested). TimingView's
    revisions come from `getCalibration` via `useControlResource`.
  - `CalibrationPage` reads through `useControlResource`, mutates through
    `useAsyncAction`, shows `Notice`s. `RomMapsPanel`, `TrainingPanel` and the
    capture panels use `describeError`.
  - Tests first: the `useControlResource.test.ts` polling path (6 new: no
    overlap, keeps data with a described error, `intervalMs: null`,
    `describe`, disabled, abort on unmount); `CalibrationPage.test.tsx` (8,
    new; its two shared-words tests failed before the change). In
    `AnalysisPage.test.tsx`, "drops a stale logs reply that lands after the
    toggle", "clears a logs refusal once the next listing succeeds" and "keeps
    a Compare refusal out of the Cells view" failed before and pass after. In
    `Phase6Panels.test.ts`, "describes a refused job in the shared words"
    failed before and passes after.
  - `AnalysisPage.test.tsx` (11), `LogsPage.test.tsx` (13) and
    `RomMapsPanel.test.tsx` (20) pass. Two call assertions gained
    `expect.anything()` for the `{signal}` argument the routes now pass.

- [x] **R08.3 Route every localStorage access through `util/storage`.**
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

  **Done 2026-09-25.** `TestSessionPanel` uses `readString`/`writeString`.
  `TelemetryPanel.loadStoredKeys` and `useTagQueue.loadQueue`/`saveQueue` use
  `readJson`/`writeJson`, and the tag queue now validates each stored row, so
  a malformed row is dropped instead of breaking a later render.
  `TestSessionPanel.test.ts` (new) renders with a `localStorage` getter that
  throws `SecurityError`. It failed before (`Uncaught SecurityError`) and
  passes after. `useTagQueue.test.ts` (new, 4) covers restore, junk storage and
  throwing storage. `TelemetryPanel.test` 18/18.

## Structure

- [x] **R08.4 One function per route: move API calls out of panels and
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

  **Done 2026-09-25.** `api/workbench.ts` has one function per route: getLogs,
  getFrame, getDeliveryError, getTrimSurface, getTiming, getPullComparison,
  getCalibration, recordFlash, getMemoryChannels, verifyMemoryChannels,
  inspectRom, getCellResidency. Each takes an optional `signal`.
  `types/workbench.ts` holds every workbench type, including `RomInspection`,
  `RomTable`, `CellResidency`, `TableTrace` and a single `CalibrationResponse`.
  It is re-exported from the `types` barrel, and `lib/romMaps` and
  `lib/romTrace` re-export their types so old imports work. `RomMapsPanel`
  uses the existing `getCaptureProfiles`. `api/core.queryString()` replaced
  `workbenchApi.query()` and is used by `getBinnedAnalysis` and
  `getSessionChart`. The required `value` and `x` of `getBinnedAnalysis` are
  always sent, even empty (`value=`), as at HEAD, so a cleared Explore field
  gets the route's own answer rather than a 422 for a missing parameter
  (`core.test.ts` "always sends the required value and x, even empty").
  **Behaviour change:** `getSessionChart` now omits `channels=` when the
  channel list is empty. The panel never loads with no channels, so this is
  harmless. `toCsv` moved to `util/csv.ts`. `lib/workbenchApi.ts`
  keeps only `describeRefusal`, `gateSentence` and `REGIMES`.
  `src/layering.test.ts` fails on any `controlJson`/`fetchJson` outside
  `api/`. The `workbench helpers` tests in `LogsPage.test` import from the
  new homes.

- [x] **R08.5 Sweep the post-July formatter re-rolls into `util/`.**
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

  **Done 2026-09-25.**
  - `util/time.formatClockDuration(seconds, {rounding, hours, padMinutes,
    missing})` replaces `workbenchApi.formatDuration` (the log browser keeps
    rounded `m:ss` and "unknown") and the body of
    `TuningContextBar.elapsedLabel` (`hours: "always"`). `formatElapsed`
    delegates to it. `util/time.test.ts` (new, 4).
  - `util/format` exports `KM_PER_MILE`/`M_PER_FT` once. `GpsTab` uses
    `formatAltitude`/`formatGpsSpeed`/`formatHeading`. **Behaviour change:**
    the GPS tab heading now reads "90° E", like every other heading
    (`DataSourcesPanel.test` asserts it). `TripPanel` imports `KM_PER_MILE`,
    and its `channelValue` is `numberOrNull(...)`.
  - `util/format.formatReading(value, {digits, trimZeros, bigAt, missing})`
    replaces LogsPage `formatValue`, DataSources `trimNum`, Charts `fmt`, the
    body of `behaviorOverlay.fmt` and the capture runner's `toFixed(2)`. Each
    keeps its output, and `format.test.ts` pins every caller's options.
  - `lib/romMaps.streamKey(spec)` is the only place `obd:mem:` is written.
    `layering.test.ts` fails on a hand-built one.
  - `LogsPage.test` ("formats durations"), `TuningContextBar.test` (12),
    `format.test.ts` (13), `DataSourcesPanel.test` (8) and `TripPanel.test`
    (9) pass.

- [x] **R08.6 Split `CaptureProfilesPanel` (840 lines) into runner and
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

  **Split done 2026-09-25. The rate unification waits on the operator
  decision.**
  - `hooks/useCaptureCatalog(token)`: profiles, resolved plans, pids, dids and
    memory. A failure reads through `describeError`.
  - `CaptureRunner.tsx` (Engine Tuning → Logging) holds the optimistic
    snapshot, the pure and tested `capturePhase(busy, active)`, the Space-key
    pause, live values, the verdict and the scheduler table. It has no draft,
    so it can only launch a saved profile by name.
  - `CaptureProfileEditor.tsx` (System → Capture profiles) holds the draft,
    toggles, `ChannelPicker`, save and delete. It has no Start.
  - Shared pieces are in `captureProfileParts.tsx` (`Field`, `ProfilePicker`,
    `CollectionStatus`, `CapturePlanSummary`). `CaptureProfilesPanel.tsx` and
    its test are deleted, since no caller rendered the combined mode.
  - `lib/scheduler.ts` exports `matchingRequest` and `effectiveTargetHz`
    (TelemetryPanel) and `requestedRateHz` (the runner table) side by side.
    Each keeps its current semantics. `lib/scheduler.test.ts` (3).
  - The tests now target the shipped modes: `CaptureRunner.test.ts` (18) and
    `CaptureProfileEditor.test.ts` (13). `LoggingPage.test.tsx` (6, new) is
    behavioural. It checks that Logging shows the runner and not the editor,
    that tags stay disabled until a capture opens, that a tag lands at tap
    time, the tag-status error, the KOEO and storage warnings, and
    navigation. The four effects that set state are gone from the split, so it
    is lint-clean without suppressions.
  - ~~Remaining: unify the "requested" rate once the operator decides.~~

  **Rate unified 2026-09-25** (operator decision, R00: a windowed DID's
  "requested" rate is the window cadence it is actually polled at).
  - Both panels now read `effectiveTargetHz(request)`; `requestedRateHz` is
    gone. It returns the daemon's `configured_rate_hz`: since R02.3 one policy
    (`_request_rate_hz`) feeds the scheduler rows, the resolved plan and the
    preview, and for a windowed DID that is one read per window *period*
    (profile cycle → config cycle → profile interval → config interval),
    scaled by a fractional weight.
  - The old `effectiveTargetHz` re-derived `1 / did_profile.window_interval_seconds`.
    That field is the end-to-start *interval* only, so it disagreed with the
    plan whenever a cycle governs. Example: a 0.5 s profile cycle beside the
    config's 60 s interval showed 0.017 Hz for a DID read at 2 Hz. It also
    ignored the weight: 0.5 Hz shown against a plan of 0.333 Hz. The runner
    table was already right. TelemetryPanel's freshness chips were the ones
    that drifted.
  - Pinned against the backend: `src/test/fixtures/windowedDidScheduler.json`
    is a status slice (request rows, `did_profile`, `resolved_plan.members`)
    for two scenarios: config cycle beats profile interval at weight 0.5, and
    a profile cycle at weight 1. `tests/test_dashboard_scheduler_fixture.py` (2)
    rebuilds it from the live policy and fails if the policy moves.
    `lib/scheduler.test.ts` (5) asserts that the requested rate equals the
    plan member's `rate_hz` (4 dp), is at most one read per window, and is
    not `1 / interval`. `CaptureRunner.test.ts` "requests a windowed DID at its
    window cadence…" renders the runner table and reads `2.00` in the
    requested column.

- [x] **R08.7 One chart palette that follows the theme.**
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

  **Done 2026-09-25.**
  - `lib/chartTheme.ts` provides:
    - `ColorToken`, and `cssVar(token)` for SVG and DOM styles;
    - `cssColor(token)` via `getComputedStyle` for canvas, falling back to
      `DAY_TOKENS` (which `themeClasses.test.ts` pins to `index.css`);
    - `withAlpha`;
    - one `SERIES_TOKENS` palette: accent, accent-2, ok, bad, and two new
      tokens `--color-series-5/6` with night variants;
    - `useThemeVersion()`, so canvas effects repaint when `data-theme` flips.
  - `lib/colorScale.ts` provides `sequential` (the ROM map's green→red, now
    shared), `diverging` (blue / neutral / red, centred on zero) and
    `coverage`. `romMaps.mapColor`, `romTrace.coverageColor`, `Heatmap`,
    `CellGrid` and the Logs scatter and heat ramps use them. `CellGrid` takes
    `scale="diverging"` for the trim surface and the revision comparison.
  - SVG uses `stroke-*`/`fill-*` classes or `cssVar`. Canvas (Curve, Heatmap,
    Sparkline, MultiSeriesChart) uses `cssColor`. `Sparkline.stroke` and the
    behaviour severity map (`SEVERITY_TOKEN`, was `SEVERITY_HEX`) take tokens.
    `themeClasses.test.ts` fails on a literal `stroke`/`fill`/`*Style` colour
    or an `hsl(`. `canvasCharts.test.tsx` (12, written before the change)
    asserts token colours and a repaint on a theme flip.
  - Screenshots in day and night were taken with Playwright against the
    production bundle, from a scratch spec. Before and after: the
    delivery-error chart, the trim surface, the Logs strips, scatter and heat
    map, and the ROM map with its coverage overlay. After only: the session
    chart (canvas). The night captures show the dimmer tokens. The trim
    surface is now blue/neutral/red around zero, in the same family as the ROM
    map. `e2e/romMaps.spec.ts` passes, colour assertion included.
  - **Behaviour changes:**
    - trim-surface and timing-comparison cells diverge around zero instead of
      following an extent ramp;
    - Behavior heat maps and the Logs heat map and scatter use the green→red
      sequential ramp, where they had teal→amber→white and an HSL rainbow.
  - Behavior page, day and night: a scratch Playwright spec served the real
    `/home/dan/canary-data/behavior_models_2026-09-22.json` (6 `surface_2d`,
    2 `surface_1d`) on `/models/behavior`, with a synthetic live overlay.
    Sampled pixels confirm the canvas follows the theme. The fitted curve is
    `#ffb547` by day and `#d99a3c` at night. The warning Δ-trail sparkline is
    `#ffd166` by day and `#d9b154` at night. Panel backgrounds go from
    (22,29,36) to (14,19,24). The heat-map cells are identical in both
    themes, as intended: ramps encode data, not chrome. The absolute-band
    envelope on `cat_warmup` and the severity-coloured live cell outlines
    render in both themes.

- [x] **R08.8 Split `RomMapsPanel`.**
  - The 7-branch `gateReason`/`traceReason` chain is inline.
  - The coverage overlay is a state machine spread over four keyed `Record`
    states: `coverageOn`, `coverageSource`, `residency` and `liveCoverage`.
    `fetchResidency`, `coverageFor` and a refetch effect belong to it.
  - `MapTable` sits in the same file.

  **Do:** a pure `traceGate(...)` in `lib/romTrace.ts` with unit tests, a
  `useCoverageOverlay` hook, and `MapTable.tsx` in its own file. **Accept:**
  the 20 vitest tests and `e2e/romMaps.spec.ts` pass. Size **M**.

  **Done 2026-09-25.**
  - `lib/romTrace.traceGate({confirmed, rom, status, now, fuelCut, rpm, bfs})`
    returns `{gate, trace}`; the first failing reason wins. `romTrace.test.ts`
    gains 5 tests: every gate in order, stale status, live-input reasons kept
    out of the shared gate, and the 750 ms skew edge.
  - `hooks/useCoverageOverlay.ts` owns the on/source/residency/live-grid
    state. Its residency reads are declarative: an effect requests what the
    open overlays need, and toggling retries. Live coverage is keyed by
    capture instead of being reset in an effect.
  - `MapTable.tsx` is its own file.
  - `RomMapsPanel.test.tsx` 20/20; `pnpm e2e` 6/6, including
    `e2e/romMaps.spec.ts`.

- [x] **R08.9 One poll loop.**
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

  **Done 2026-09-25.** `hooks/usePolling.ts` schedules with completion-chained
  `setTimeout`, aborts on unmount or a parameter change, never reports an
  `AbortError`, keeps the last data across a failure, and fetches once for
  `intervalMs: null`. `useControlResource`, `useStatus`, `useBehaviorModels`
  and `useBehaviorLive` are built on it. Tests first: `useStatus.test.ts` (6)
  passed on the old loops and passes on the new one. In
  `useControlResource.test.ts`, "never starts a poll while the previous one is
  still in flight" failed on `setInterval` and passes now. With fake timers,
  10 s with one request pending makes 1 call, and the next poll starts 2.5 s
  after it settles. `ModelHealthPanel` shows `describeError(statusError)`, and
  `ModelEvidenceHealth.test.ts` asserts the unreachable-daemon wording.

- [x] **R08.10 Shared file-download helper.**
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

  **Done 2026-09-25.** `util/browser.downloadBlob(blob, name)` and
  `downloadText(content, name, mime)` replace all five copies (Charts,
  MultiSeriesChart, Logs, SessionsCard, CustomDashboard).
  `api/core.controlBlob(path, token, init, baseUrl)` backs `downloadSession`,
  which gains `baseUrl`, and `downloadSessionTrack`.
  `MultiSeriesChart.test.tsx` (4, new, written before the change) covers CSV
  content with and without timestamps, toolbar visibility and the hover
  readout. `core.test.ts` gains 2 tests for `controlBlob`. `ExportPanel.test`
  (3) and `ChartsPanelSessionMode.test` (3) pass.

- [x] **R08.11 Delete dead code and add one segmented-control component.**
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

  **Done 2026-09-25.** `components/SegmentedControl.tsx` is a labelled
  `role="group"` of toggle buttons with the chosen one `aria-pressed`; the
  button look is unchanged. AnalysisPage's views and Timing mode and
  LogsPage's presentation use it. The orphaned `groupTabClass` and
  `groupTabsClass` were deleted, along with `dimTextClass`, `nav.groupOf`,
  `http.getConfig` and `http.getLatestTelemetry`. The Analysis-view buttons
  were `role="tab"`, so `AnalysisPage.test.tsx` and `Workbench.test.ts` now
  query them as buttons. `SegmentedControl.test.tsx` (2).

- [x] **R08.12 Low-value mechanical sweeps (batch them, or skip them).**
  - Ten files hand-write the `panelClass` + `<header>` + `<h2>` shell instead
    of using `<Panel title count actions className>`: ControlPanel,
    ControlPanelSections, JobsCard, SessionsCard, ReplayCard,
    ModelUploadCard, SequenceDetectorCard, TestSessionScreens,
    BehaviorModelsPanel and CaptureProfilesPanel.
  - The 8-line `devicePixelRatio` canvas setup is repeated in `Sparkline`,
    `Curve`, `Heatmap` and `MultiSeriesChart`. Add `prepareCanvas(canvas, w,
    h)`.

  Size **S–M**.

  **Mostly done 2026-09-25.** `lib/canvas.prepareCanvas(canvas, w, h)` is used
  by Sparkline, Curve, Heatmap and MultiSeriesChart; `canvasCharts.test.tsx`
  and `MultiSeriesChart.test.tsx`, written first, pin their behaviour.
  `<Panel>` now wraps ControlPanelSections (Preflight, Control Events),
  JobsCard, ModelUploadCard, SequenceDetectorCard, TestSessionScreens (3
  panels), BehaviorModelsPanel, SessionsCard, ReplayCard and the capture
  runner and editor. `panelShells.test.tsx` (8) was written first and still
  passes, and so does `SessionsPanel.test.ts`.
  **Remaining:** `ControlPanel`'s Capture section, because the panel has no
  behavioural test yet and needs one before the change.
  *Remainder done 2026-09-25.* `ControlPanel.test.tsx` (3) was written first
  and passed against the hand-written shell: the Capture header reads
  `idle`/`active` (with its colour), Start sends the typed purpose/protocol/
  vehicle state, Tag trims its label and Stop sends the reason on an open
  window, and without a token every action is disabled and nothing is
  fetched. The section is now `<Panel title="Capture" actions={…}>`;
  `ControlPanel.tsx` no longer imports `panelClass`/`panelHeaderClass`/
  `panelTitleClass`. Gate: `pnpm typecheck`, `pnpm lint`, `pnpm format:check`
  clean; `vitest run src/panels/ControlPanel.test.tsx
  src/panels/panelShells.test.tsx` 11 passed.

- [x] **R08.13 A linter and a formatter for the dashboard.** *Operator,
  2026-09-24: "seems like we should use a linter right".* The dashboard had
  no linter and no formatter: 154 lines ran over 140 characters, and nothing
  checked promise handling or hook dependencies. The first ESLint run found
  7 floating promises and 6 async callbacks handed to timers or listeners.

  **Done 2026-09-24:**
  - Prettier 3.9 (`printWidth: 100`, like ruff; the default double quotes
    and semicolons match the existing code). One mechanical
    `prettier --write` commit over 113 files is listed in
    `.git-blame-ignore-revs`. After it: typecheck clean, vitest 47 files and
    357 tests pass.
  - ESLint 10 with typescript-eslint 8.70 `recommendedTypeChecked`,
    eslint-plugin-react-hooks 7.1 `recommended-latest` (including the React
    Compiler rules) and eslint-config-prettier.
    - Every rule is an error: the preset's three hook warnings are raised,
      because warnings neither fail nor bulk-suppress.
    - Two scoped relaxations, both reasoned in `eslint.config.js`: async JSX
      event handlers (48 hits; React ignores the return value), and the
      `no-unsafe-*`/`require-await`/`unbound-method` rules in vitest files,
      where mocks are `any` by construction.
  - The baseline is bulk-suppressed in `eslint-suppressions.json`: 78
    violations in 32 files. The largest groups:
    - `react-hooks/set-state-in-effect` 23 (the fetch-in-effect pattern, R08.2)
    - `refs` 9
    - `no-floating-promises` 7
    - `no-misused-promises` 6
    - `no-unnecessary-type-assertion` 6
    - `exhaustive-deps` 6
    - `purity` 5
  - The ratchet was verified three ways:
    - a new floating promise fails;
    - a stale suppression fails, naming `--prune-suppressions`;
    - linting only the changed files works with the suppressions (3.4 s,
      against 39 s for the whole dashboard).
  - Gate: `scripts/ci_gate.sh` lints and format-checks the changed files, or
    the whole dashboard when its lint/format/TS config or dependencies
    change. CI (`frontend` job) runs `pnpm lint` and `pnpm format:check` in
    full. AGENTS.md is updated.

  **Remaining:** burn the baseline down. The hook items (R08.2, R08.8, R08.9)
  should remove most of `set-state-in-effect`, `refs` and the promise rules.
  Each prunes the suppressions it clears. This box goes `[x]` when
  `eslint-suppressions.json` is empty or every entry left is named here as
  deliberate.

  **Burn-down 2026-09-25 (R08.1–R08.12 pass): 78 → 35 violations, 32 → 20
  files**, pruned with `pnpm lint:prune`. Cleared:
  - every suppression in `useControlResource`, `useStatus`,
    `useBehaviorModels`, `useBehaviorLive`, `useTagQueue`, `workbenchApi`,
    `CalibrationPage`, `LogsPage`, `TuningContextBar`, `RomMapsPanel.test` and
    `Curve`, and the deleted `CaptureProfilesPanel`;
  - 4 of 6 in `RomMapsPanel`, and 4 of 7 in `TestSessionPanel` and
    `TelemetryPanel` together.

  Left:

  | Rule | Count | Files |
  |---|---|---|
  | `set-state-in-effect` | 11 | ControlPanel, ReplayCard ×2, RomMapsPanel (per-sample tally effect), SessionsPanel ×2, TelemetryPanel (pin seeding), TestSessionPanel ×2, TuningPanel, useControlStream |
  | `refs` | 6 | MonitorsPanel ×3, RescoreControl ×2, TripPanel |
  | `purity` | 3 | AlarmsPanel, TripPanel ×2 |
  | promise rules | 3 | useConnectivityMode ×2, ProfilesPanel |
  | `no-base-to-string` | 3 | evidence, trainingQuality, TripPanel |
  | `no-unnecessary-type-assertion` | 2 | BehaviorModelsPanel.test, SessionsPanel.test |
  | `exhaustive-deps` | 2 | ControlPanel, RomMapsPanel |
  | `preserve-caught-error` | 2 | ReplayCard |
  | `websocket.ts` | 2 | `no-unsafe-argument`, `no-unused-vars` |
  | `no-redundant-type-constituents` | 1 | AnomalyPanel |

  None of these is deliberate yet.

  **Baseline burned to zero 2026-09-25: 35 → 0 violations.**
  `eslint-suppressions.json` is `{}` after `pnpm lint:prune`, and `pnpm lint`
  is clean with no suppressions. By rule:
  - `set-state-in-effect`: state is now adjusted while rendering, on the
    change itself (React's previous-value pattern, as `usePolling` already
    does). This covers ControlPanel, SessionsPanel, useControlStream (token
    clear), ReplayCard (selection follow, poll mirror), TestSessionPanel
    (profile settle, purpose prefill keyed on whether a capture is open),
    TelemetryPanel (pin seed) and TuningPanel (seed keyed on version).
    ControlPanel's and SessionsPanel's polls are now effect-local loads with
    a `cancelled` guard. ControlPanel's five states are one `ControlSnapshot`.
    RomMapsPanel's per-sample tally and live coverage step while rendering,
    guarded by a `seen` state, with absolute writes. `useCoverageOverlay.recordLive`
    now takes a batch.
  - `refs`: MonitorsPanel's baseline and RescoreControl's last event key
    are now state.
  - `purity`: new `hooks/useNow`, used by AlarmsPanel, TripPanel and
    CaptureRunner's `useCaptureAge`.
  - promise rules: `void` where the promise cannot reject (useConnectivityMode,
    ProfilesPanel's Enter key).
  - `no-base-to-string`: new `util/json.scalarText` (evidence,
    trainingQuality, TripPanel's DTC codes).
  - `preserve-caught-error`: ReplayCard rethrows with `{ cause }`.
  - The rest: websocket (text frames only, unused binding), AnomalyPanel
    (`string` for `string | "all"`), and the two test-file assertions.

  New tests:
  - `util/json.test.ts` (2).
  - `RomMapsPanel.test.tsx` "steps each ECM sample once under StrictMode's
    double render".
  - `TuningFeedback.test.ts` "keeps an in-progress edit across a same-version
    re-read; re-seeds on a new version". It fails on the old code: the old
    seeding effect depended on the polled object, so every 30 s poll
    overwrote the operator's edit.

  Gate: `pnpm typecheck`, `pnpm lint`, `pnpm format:check` clean; `pnpm test`
  64 files / 472 tests; `pnpm e2e` 6 passed.

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

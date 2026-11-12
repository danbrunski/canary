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

- [ ] **R28.1 Every WebSocket telemetry message re-renders the whole app.**
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
  - [ ] Measure first: messages per second on a connected tab during a
    `tuning` capture, and commit count from the React Profiler on the
    Pi-served build on a phone. Record both here. The structure is verified;
    the cost is not.
  - [ ] Coalesce in the hook: keep ingesting into the ref, flush to state at
    most once per animation frame (or a stated interval). Pin with a
    fake-timer test: N messages inside one window make one state update.
  - [ ] If the measurement still shows whole-tree commits: expose a
    `useChannel(key)` subscription (`useSyncExternalStore`) so only readers of
    a key re-render, and `memo` the Workbench pages that take no channels.
  - Must not change: what is stored per channel, the seed-then-stream order,
    `useTelemetryStream.test.ts`.

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

# R19 — Logs and Analysis page composition

Opened 2026-10-14. Shared rules: [R10](R10-refactor-README.md).
Size M. Independent of backend refactors; preserve existing API contracts.

## Problem and target

`LogsPage.tsx` was 690 lines: browser, query/pagination controls, virtualized
table, time-series strips, scatter view and heatmap calculations.
`AnalysisPage.tsx` was 688 lines: session selection, delivery, trim and timing
views, charts, noise floors and pairing/delay notices.

Extract the existing view components into focused files under `panels/tuning/`
and move pure chart-data preparation to `lib/`. The page files own composition
and page-level selection. Continue using `useLogs`, `useControlResource` and
`useAnalysisRun`; new polling or request frameworks are unnecessary.

## Implementation checklist

- [x] **R19.1 Map view ownership and preserve behavior.** (Close-out:
  "Ownership map at the move" and "Behaviour record".)
  - [x] Inventory exported components, imports from tests/other panels,
    component state, localStorage keys, request hooks and CSS dependencies.
    Recorded below. Outside the two page files the only importers were
    `Workbench.tsx` (`LogsPage`, `AnalysisPage`), `LogsPage.test.tsx`,
    `AnalysisPage.test.tsx` and `DynoView.test.tsx` (`AnalysisPage`); nothing
    imported `LogBrowser`, `LogViewer`, `FrameTable`, `FrameVisual`,
    `MEASURED_SOURCES` or `DeliveryChart`.
  - [x] Record Logs browser selection, query draft/applied values, regimes,
    pagination resets, sorting, virtualization and export behavior.
    "Behaviour record: Logs" below; each row is pinned by a test named there.
  - [x] Record Analysis session/revision selection, tab behavior, independent
    results/refusals, running-calibration defaults and notices.
    "Behaviour record: Analysis" below.
  - [x] Use existing tests/e2e as baselines; add missing coverage only for
    meaningful interactions endangered by moving state/component boundaries.
    All 13 `LogsPage.test.tsx`, 13 `AnalysisPage.test.tsx`, 6
    `DynoView.test.tsx` and 13 `Workbench.test.ts` tests pass unmodified in
    their assertions on the refactored tree (their fixtures moved to
    `src/test/workbenchFixtures.ts` first, re-run green at HEAD). Before any
    code moved, a rendered-DOM + request-log capture of 111 states was taken at
    HEAD (below); the missing interaction coverage was written at the page level
    against HEAD first, then moved to the owning components.
- [x] **R19.2 Extract Logs components.**
  - [x] Separate `LogBrowser`/row presentation, `LogViewer` controls/request
    state, `FrameTable`, and visual/scatter presentation into coherent modules.
    `LogBrowser.tsx` (+ `LogRow`), `LogViewer.tsx`, `FrameNotes.tsx`,
    `FrameTable.tsx`, `FrameVisual.tsx` (strips), `ScatterView.tsx`.
    `LogsPage.tsx` 690 -> 27 lines.
  - [x] Keep pagination/request identity in its current owner and preserve
    stale-result masking, cancellation and refusal behavior. `LogViewer` still
    owns `viewKey`, the `{key, offset}` page state and the `getFrame` fetcher;
    `FrameNotes` only calls `onOffset`. `LogViewer.test.tsx`: "pages by the row
    cap and starts again at row 0 when the view changes" (grid, regime,
    channels, log; asserts the only request sent for the new view has
    `offset=0`), "keeps the old frame under Loading… and drops a late reply
    from a superseded view" (also asserts the superseded request's signal is
    aborted), "replaces the frame with a refusal and brings it back with the
    next success".
  - [x] Preserve virtualized row height/overscan, sorting, row accessibility
    and scroll behavior; do not replace the table with a new library.
    `FrameTable.test.tsx` (window of 58 rows at the top, `80..138` at
    `scrollTop` 2400, 21 rows and no bottom spacer at the end, spacer heights,
    sorted order windowed, 460 px cap, `tabindex=0`, sort arrows, nulls last in
    both directions); e2e `logsAnalysis.spec.ts` and `phone.spec.ts` (2000 rows
    render <= 58 `tr[data-row]` in real Chromium, last row reachable by
    scrolling the box). No library was added.
  - [x] Keep selection/context and persistent spec keys stable across mounts.
    `canary.tuning.logSpecs.v1` unchanged (`LogViewer.test.tsx`: "applies the
    channel draft only on Apply, and a later mount reads it back", "keeps the
    same storage key across mounts"); `LogBrowser.test.tsx` "marks the selected
    capture and reports a click"; `LogsPage.test.tsx` "opens the viewer for the
    selected capture and follows a new selection".
  - [x] Retain old exports where tests or consumers need them during migration.
    `LogsPage.tsx` re-exports `LogBrowser`, `LogViewer`, `FrameTable`,
    `FrameVisual` directly from their owners (none had an importer; kept per
    R10). `LogsPage.test.tsx` "re-exports the components it was split into from
    their old path" asserts identity.
- [x] **R19.3 Extract pure plot-data helpers.**
  - [x] Move extent/range, marker, scatter and heat-bin preparation where it
    removes substantial rendering logic; keep React state in components.
    `src/lib/framePlot.ts` (`extent`, `fraction`, `timeAxis`/`timeX`,
    `stripPoints`, `stripMarkers`, `scatterPoints`, `heatCells`, `heatShade`),
    `src/lib/frameTable.ts` (`sortedRowOrder`, `visibleRowWindow`,
    `formatFrameValue`, `frameCsv`, `frameCsvName`), `src/lib/analysisPlot.ts`
    (`deliveryScale`, `trimCells`, `comparedAsCells`). Axis choices, sort,
    scroll position, mode and memoization stay in the components.
  - [x] Reuse existing canvas/color/theme helpers instead of introducing
    another palette, renderer abstraction or formatting system. `sequential`,
    `sequentialIn`, `cssVar`, `seriesVar` (colorScale/chartTheme) and
    `formatReading` (util/format) are used as before; no colour, renderer or
    formatter was added.
  - [x] Preserve null/non-finite handling, constant-range scaling, time order,
    bin boundaries, density versus mean coloring and unit labels.
    `framePlot.test.ts`: `extent` skips null/NaN/±Infinity, `fraction` of a
    zero-width range is 0 not NaN, bins clamp the maximum into cell 11, a value
    on an edge falls in the upper bin, density vs mean, a sample missing a
    colour value is not counted, first-seen cell order; `frameTable.test.ts`:
    nulls last both ways, ties keep original order.
  - [x] Preserve gaps in data as the current implementation treats them;
    changing interpolation or drawing across gaps is a separate UI fix.
    `framePlot.test.ts` "skips a missing sample, joining its neighbours across
    the gap" and `FrameVisual.test.tsx` "draws across a missing sample rather
    than breaking the line" pin it (mutation M08 below fails both).
  - [x] Add focused numerical helper tests for actual edge cases, not tests
    that simply restate each arithmetic expression. 53 tests over the three
    modules (28 + 14 + 11); every case listed in the bullets above is a
    boundary or degenerate input, and 22 mutations of the code under test each
    fail at least one test (below).
- [x] **R19.4 Extract Analysis views.**
  - [x] Separate `SessionChooser`, delivery, trim and timing views; keep
    `AnalysisPage` as a small composition layer over its existing choice/view
    state. `SessionChooser.tsx`, `DeliveryView.tsx`, `TrimView.tsx`,
    `TimingView.tsx`; `AnalysisPage.tsx` 688 -> 58 lines and still owns only
    `choice`, `view` and `runningRevision`.
  - [x] Group delivery chart, pairing/delay notices and noise-floor notes by
    their consumers. Share a notice only where its semantics actually match.
    `DeliveryChart.tsx` and `DeliveryNotes.tsx` (`PairingNote`,
    `DelayStability`) are consumed by `DeliveryView` only; `NoiseFloorNotes.tsx`
    holds `FloorNote` (shared: delivery result and revision comparison state the
    floor the same way) and `SignificanceNote` (comparison only, falls through
    to `FloorNote` when the floor was accepted).
  - [x] Preserve each view's own analysis-run state and timing modes' distinct
    refusals; do not couple previously independent in-flight requests. Each view
    keeps its own `useAnalysisRun`; `TimingView` keeps both runs and the
    `revA`/`revB`/`floorChannel` state in one component, so a mode switch loses
    nothing. `TimingView.test.tsx` "keeps a Cells run and a Compare run
    independent, in flight and after" (Compare started and finished while Cells
    is pending; Cells' signal is never aborted), "keeps each mode's refusal
    under that mode"; `AnalysisPage.test.tsx` "drops a run in flight when the
    view changes, and starts clean on return".
  - [x] Preserve unavailable floor versus refused floor, stated conditions,
    missing inputs and empty evaluated results in rendered messages.
    `NoiseFloorNotes.test.tsx` (7), `TimingView.test.tsx` floor table (no floor
    asked for / never measured / accepted / refused with none measured),
    `DeliveryView.test.tsx` ("states a noise floor that was not measured…",
    "keeps the floor and the conditions with an empty result, beside the missing
    inputs"), and the original `AnalysisPage.test.tsx` cases ("names a refused
    noise floor…", "keeps the two kinds of empty distinguishable").
  - [x] Keep HTTP functions in `api/`; follow the existing dashboard layering
    test. No new `controlJson`/`fetchJson` use; `layering.test.ts` (4, was 2)
    still green.
- [x] **R19.5 Migrate and verify.**
  - [x] Sweep imports/exports and adapt tests to exercise owning components;
    keep page-level tests for navigation/context and cross-view integration.
    New owner tests: `LogBrowser`, `LogViewer`, `FrameTable`, `FrameVisual`,
    `SessionChooser`, `DeliveryView`, `DeliveryChart`, `DeliveryNotes`,
    `NoiseFloorNotes`, `TrimView`, `TimingView` (+ three `lib/` files). Page
    tests keep the original 13 + 13 cases and gain 5 + 5 composition cases
    (token gating, shared choice across views, view switch aborting a run,
    re-export identity). `layering.test.ts` gained: nothing but
    `Workbench.tsx` imports `LogsPage`/`AnalysisPage` (static, dynamic, type
    imports), and the three `lib/` helpers import neither React nor
    `components|hooks|panels|api`.
  - [x] Run affected `LogsPage.test.tsx`, `AnalysisPage.test.tsx`,
    `Workbench.test.ts`, relevant hooks and `layering.test.ts`. All green in the
    final full `pnpm test`; new `hooks/useWorkbench.test.ts` (8: `useAnalysisRun`
    busy/keep-result/refusal/abort-on-supersede/abort-on-unmount, `useLogs`
    token gating/toggle/late reply).
  - [x] Verify rapid selection changes, aborts, loading/error transitions,
    unauthorized/absent token and zero-row results. Rapid changes and aborts:
    `LogViewer` stale-reply test, `SessionChooser` "ignores the superseded
    listing", `useWorkbench` supersede/unmount, `TimingView` independence.
    Loading/error: `Loading…` over the old frame, refusal replacing the frame,
    deferred/unreachable/401 each in their words. Absent token: `LogBrowser`,
    `LogViewer`, `LogsPage`, `DeliveryView`, `AnalysisPage` ("asks for the
    token, and keeps every Run disabled without one") issue no request.
    Unauthorized: 401 with an empty body reads "Not authorized…" in browser and
    viewer; a 401 with a `detail` string shows the daemon's sentence (current
    behaviour, pinned). Zero rows: `FrameTable`/`FrameVisual` "No rows in this
    view." with no table and no export button.
  - [x] Verify phone/desktop scrolling, table virtualization and plot sizing
    with existing browser checks; add a relevant e2e assertion if coverage lacks
    it. No existing e2e rendered Logs or Analysis with data (`emptyDaemon`
    only visits them with `{}`), so added `e2e/logsAnalysis.spec.ts` (desktop, 5
    tests: window <= 58 of 2000 rows in a 460 px scroll box, scroll to the last
    row, sorted descending, strips 64 px high and panel-wide with a 360 x 240
    scatter and heat map, delivery chart 420 x 220 for six bands) and 4 tests in
    `phone.spec.ts` at 412 px (same windowing, no sideways page scroll, visual
    presentation, 2-band delivery chart; the 6-band chart is a documented
    `test.fail()`, below). Shared stubs in `e2e/fixtures/workbenchRoutes.ts`.
    Removing the row window fails 4 of these (received 2000 rows).
  - [x] Run `pnpm typecheck`, `pnpm format`, `pnpm lint` and focused vitest
    while iterating. Prune obsolete suppressions when fixing their violations.
    Run throughout; `pnpm lint:prune` left `eslint-suppressions.json`
    unchanged (no suppression covered the moved files). 30 type-aware
    violations (unnecessary assertions) in the new tests were fixed (generic
    `getByLabelText<T>` instead of casts), none suppressed.
  - [x] With CI off, complete full `pnpm lint`, `pnpm test` and `pnpm e2e`
    from `src/dashboard/static`, and record results. Final: typecheck exit 0;
    `pnpm format:check` clean; `pnpm lint` exit 0; `pnpm test` 105 files / 872
    tests passed (baseline 90 / 691); `pnpm e2e` 31 passed + 1 skipped
    (baseline 22 passed + 1 skipped; the skip is `dashboard.spec.ts:136` on
    chromium, as before).
  - [x] Record final component map, preserved exports/state/storage keys,
    interaction evidence and commits; no API or visual redesign in this task.
    Close-out below. Implementation commit: the commit that archives this file.

## Acceptance

Page files primarily compose views. Data preparation is independently testable,
request/state ownership is preserved, and existing navigation, plots, tables,
exports, accessibility and refusal messages behave the same.

## Close-out record (2026-10-15)

### Ownership map at the move

HEAD c7c325fc. Importers outside the two page files: `Workbench.tsx`
(`<LogsPage token context>`, `<AnalysisPage token status context>`) and tests.
CSS dependencies: Tailwind utility strings only (moved verbatim) plus the
`TableScroll` (`data-table-scroll`), `Panel` and `SegmentedControl` components;
no stylesheet.

| Module | Owns | State kept there |
|---|---|---|
| `LogsPage.tsx` (27) | composition: browser, and the viewer once a log is selected and a token exists; direct re-exports of the four old exports | none (selection is the group's `TuningContext`) |
| `LogBrowser.tsx` (129) | capture listing, `LogRow`, count, empty/refusal text | `includeSessions`; `useLogs` |
| `LogViewer.tsx` (129) | channel draft/applied specs, grid, regime, presentation, request identity, page offset | `specsText` (localStorage `canary.tuning.logSpecs.v1`), `draft`, `grid`, `regime`, `presentation`, `page {key, offset}`; `useControlResource(getFrame)` |
| `FrameNotes.tsx` (70) | revision/profile/grid line, flash alert, missing channels, regime outcome, freshness bound, Previous/Next | none (`onOffset` callback) |
| `FrameTable.tsx` (105) | sortable windowed table, CSV export | `sort`, `scrollTop` |
| `FrameVisual.tsx` (70) | one strip per channel, tag/flash markers | none |
| `ScatterView.tsx` (135) | scatter / 12 x 12 heat map of two channels, optional colour channel | `xChoice`, `yChoice`, `zChoice`, `mode` |
| `lib/framePlot.ts` (171), `lib/frameTable.ts` (79) | pure data preparation for the above | none |
| `AnalysisPage.tsx` (58) | composition; direct re-exports `DeliveryChart`, `MEASURED_SOURCES` | `choice`, `view` |
| `SessionChooser.tsx` (104) | session listing, running-calibration default, selected-log shortcut | `includeSessions`; `useLogs` |
| `DeliveryView.tsx` (117), `DeliveryChart.tsx` (71), `DeliveryNotes.tsx` (38) | measured-source choice, run, result, chart, pairing / delay notices | `measured`, `minLoad`; one `useAnalysisRun` |
| `NoiseFloorNotes.tsx` (46) | `FloorNote`, `SignificanceNote` (shared by delivery and comparison) | none |
| `TrimView.tsx` (102) | trim surface by bank and split | `bank`, `split`; one `useAnalysisRun` |
| `TimingView.tsx` (171) | cells and revision-comparison modes | `mode`, `revA`, `revB`, `floorChannel`; two `useAnalysisRun`s; calibration resource (only while Compare is open) |
| `lib/analysisPlot.ts` (83) | delivery-chart scale, trim cell selection, compared-cell shaping | none |

Imports run owner-ward: no view imports `LogsPage`/`AnalysisPage`
(`layering.test.ts`). Routes stay in `api/workbench.ts`
(`getLogs`, `getFrame`, `getDeliveryError`, `getTrimSurface`, `getTiming`,
`getPullComparison`, `getCalibration`); the request hooks are `useLogs`,
`useControlResource`, `useAnalysisRun`, unchanged.

Preserved exports (old path -> still there): `LogsPage.tsx`: `LogsPage`,
`LogBrowser`, `LogViewer`, `FrameTable`, `FrameVisual`. `AnalysisPage.tsx`:
`AnalysisPage`, `DeliveryChart`, `MEASURED_SOURCES`. Storage keys:
`canary.tuning.logSpecs.v1` (LogViewer) and `canary.tuning.selectedLogId.v1`
(`useTuningContext`, untouched); no key added.

### Behaviour record: Logs

| Behaviour | Pinned by |
|---|---|
| Row click -> `context.setSelectedLogId`; selected row `aria-selected` | `LogBrowser.test` "marks the selected capture and reports a click" |
| Draft vs applied specs: typing sends nothing; Apply sends trimmed, comma-split specs and persists the raw text; empty list sends nothing and shows no "Loading…" | `LogViewer.test` (3 cases) |
| Defaults 1 s / All / row 0; grid and regime options | `LogViewer.test` "asks for the stored channels…", "offers the five grids and regimes…" |
| Offset resets to 0 on grid, regime, channels, log change, with no stray request at the old offset; Previous never below 0 | `LogViewer.test` "pages by the row cap…", "goes back no further than row 0" |
| Old frame stays under "Loading…"; superseded reply dropped and aborted; refusal replaces the frame | `LogViewer.test` |
| Sorting: time asc/desc, channel asc/desc, new column starts asc, nulls last | `FrameTable.test`, `frameTable.test` |
| Window: ROW_HEIGHT 24, 18 rows, 20 overscan, 460 px cap, spacers | `FrameTable.test`, `frameTable.test`, e2e |
| Export: every row, in displayed order, header with units, `<session>-<regime>.csv`, `text/csv` | `FrameTable.test` (3) |
| Strips, markers (ends inclusive), scatter geometry, heat bins, colour | `FrameVisual.test` (17), `framePlot.test` |

### Behaviour record: Analysis

| Behaviour | Pinned by |
|---|---|
| Choosing a session clears a revision choice; "All on the running calibration" sets the revision, disabled with a title until known; "Use the selected log" only when a log is selected | `SessionChooser.test` |
| The choice survives a view switch; each view's run does not (the view unmounts, its signal aborts) | `AnalysisPage.test` |
| Delivery: Run needs token + choice + source; `min_load` only for a Mode 01 source, typed value kept across a source switch; result kept while a rerun waits, cleared by a refusal | `DeliveryView.test` |
| Timing: Cells and Compare runs and refusals independent; revisions read only while Compare is open; Compare needs two different revisions; floor channel trimmed or omitted | `TimingView.test` |
| Notices: pairing with/without a modelled delay and bracket, unstable delay bracket (`none` for a missing estimate), floor never measured / measured / refused | `DeliveryNotes.test`, `NoiseFloorNotes.test`, `TimingView.test` |

### Baseline and equivalence

Before the first move a scratch vitest harness rendered `LogsPage` /
`AnalysisPage` through 93 states (browser: default, selected, sessions, both
empties, no token, refusal; table: default, four sort states, nulls both ways,
capped and paged, boundary / missing / regime notes, zero rows, deferred and
generic refusals, loading, 500-row windows at five scroll positions; visual:
default, heat density / mean, coloured and swapped scatter, markers, constant /
null / single-row data; specs / grid / regime requests; chooser, delivery x 11,
min load, trim x 8, timing cells and compare x 8, compare floor variants x 6, dyno) and recorded the
request list (path, token) of each of 18 flows: 111 files, rendered `innerHTML`
split per tag. Dumped twice at HEAD (identical), then after the refactor, and
again on the final tree (`diff -r`: identical, 111 files; digest of the
per-file sha256s `2a9c5905…bf62f`). The harness is not committed (it would rot
with the markup); its sensitivity was checked: breaking the heat-cell `y`
changed four dumps. Existing tests are the committed baseline.

### Mutation checks

22 one-line mutations of the refactored code, each run against its tests, each
reverted; every one fails at least one test (failures in parentheses):
overscan 20 -> 10 (7); nulls sorted first (4); offset not reset on regime change
(1); refusal not replacing the frame (1); no "Loading…" over a stale frame (1);
heat maximum not clamped to the last bin (4); constant range divides by zero
(7); strip draws a gap as zero (4); marker end exclusive (2); Compare showing
the Cells refusal (1); `min_load` sent for a semantic source (1); refused floor
drawn as a floor (3); session choice keeping the revision (1); CSV of the window
only (1); delivery minimum span 3 -> 2 (5); a view importing `LogsPage` (1);
viewer mounted without a token (1); a plot helper importing React (1); heat
value always the count (2); time sort reversed (6); unmount not aborting a run
(3); scatter skipping only a null x (3). In the browser, removing the row window
fails the four windowing e2e tests (2000 rows rendered).

### Commands (src/dashboard/static, final tree)

- `pnpm typecheck` exit 0 (app + e2e); `pnpm format:check` clean; `pnpm lint`
  exit 0 (`pnpm lint:prune`: no change to `eslint-suppressions.json`).
- `pnpm test`: 105 files, 872 tests passed (baseline 90 / 691). The jsdom
  `act(...)` warning count in the output is 45 before and after.
- `pnpm e2e`: 31 passed, 1 skipped (baseline 22 + 1); the skip is the
  pre-existing chromium skip of `dashboard.spec.ts:136`.
- No Python changed: `ruff check .` and `ruff format --check .` clean.

### Behaviour changes

None to the rendered DOM or the requests (111-file comparison). One internal
change: `ScatterView` now memoizes the axis ranges, so its heat-map memo can hit
(before, the ranges were new arrays every render and the memo never did);
results are identical.

### Findings left alone (not R19 behaviour; separate fixes)

- `DeliveryChart` is `bands * 70` px wide (min 320) in no horizontal
  scroller, so a six-band result is 420 px and reaches 445 px on a 412 px phone
  (the shell clips the rest). Measured in `phone.spec.ts`; recorded as a
  `test.fail()` that starts failing the run, with the instruction to delete it,
  when the chart is wrapped.
- `public/sw.js` `isDynamic()` lists `/status /config /anomalies /telemetry /obd
  /bindings /models /control /ws` but not `/analysis`, so the service worker
  treats `GET /analysis/*` as a cacheable asset (cache-first, stored when `ok`).
  Read from the code and seen as the reason Playwright's route stubs never saw
  `/analysis/*` until `serviceWorkers: "block"`; not reproduced against a live
  daemon. If it holds, a repeated identical analysis query is answered from the
  cache rather than the daemon.

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

- [ ] **R19.1 Map view ownership and preserve behavior.**
  - [ ] Inventory exported components, imports from tests/other panels,
    component state, localStorage keys, request hooks and CSS dependencies.
  - [ ] Record Logs browser selection, query draft/applied values, regimes,
    pagination resets, sorting, virtualization and export behavior.
  - [ ] Record Analysis session/revision selection, tab behavior, independent
    results/refusals, running-calibration defaults and notices.
  - [ ] Use existing tests/e2e as baselines; add missing coverage only for
    meaningful interactions endangered by moving state/component boundaries.
- [ ] **R19.2 Extract Logs components.**
  - [ ] Separate `LogBrowser`/row presentation, `LogViewer` controls/request
    state, `FrameTable`, and visual/scatter presentation into coherent modules.
  - [ ] Keep pagination/request identity in its current owner and preserve
    stale-result masking, cancellation and refusal behavior.
  - [ ] Preserve virtualized row height/overscan, sorting, row accessibility
    and scroll behavior; do not replace the table with a new library.
  - [ ] Keep selection/context and persistent spec keys stable across mounts.
  - [ ] Retain old exports where tests or consumers need them during migration.
- [ ] **R19.3 Extract pure plot-data helpers.**
  - [ ] Move extent/range, marker, scatter and heat-bin preparation where it
    removes substantial rendering logic; keep React state in components.
  - [ ] Reuse existing canvas/color/theme helpers instead of introducing
    another palette, renderer abstraction or formatting system.
  - [ ] Preserve null/non-finite handling, constant-range scaling, time order,
    bin boundaries, density versus mean coloring and unit labels.
  - [ ] Preserve gaps in data as the current implementation treats them;
    changing interpolation or drawing across gaps is a separate UI fix.
  - [ ] Add focused numerical helper tests for actual edge cases, not tests
    that simply restate each arithmetic expression.
- [ ] **R19.4 Extract Analysis views.**
  - [ ] Separate `SessionChooser`, delivery, trim and timing views; keep
    `AnalysisPage` as a small composition layer over its existing choice/view state.
  - [ ] Group delivery chart, pairing/delay notices and noise-floor notes by
    their consumers. Share a notice only where its semantics actually match.
  - [ ] Preserve each view's own analysis-run state and timing modes' distinct
    refusals; do not couple previously independent in-flight requests.
  - [ ] Preserve unavailable floor versus refused floor, stated conditions,
    missing inputs and empty evaluated results in rendered messages.
  - [ ] Keep HTTP functions in `api/`; follow the existing dashboard layering test.
- [ ] **R19.5 Migrate and verify.**
  - [ ] Sweep imports/exports and adapt tests to exercise owning components;
    keep page-level tests for navigation/context and cross-view integration.
  - [ ] Run affected `LogsPage.test.tsx`, `AnalysisPage.test.tsx`,
    `Workbench.test.ts`, relevant hooks and `layering.test.ts`.
  - [ ] Verify rapid selection changes, aborts, loading/error transitions,
    unauthorized/absent token and zero-row results.
  - [ ] Verify phone/desktop scrolling, table virtualization and plot sizing
    with existing browser checks; add a relevant e2e assertion if coverage lacks it.
  - [ ] Run `pnpm typecheck`, `pnpm format`, `pnpm lint` and focused vitest
    while iterating. Prune obsolete suppressions when fixing their violations.
  - [ ] With CI off, complete full `pnpm lint`, `pnpm test` and `pnpm e2e`
    from `src/dashboard/static`, and record results.
  - [ ] Record final component map, preserved exports/state/storage keys,
    interaction evidence and commits; no API or visual redesign in this task.

## Acceptance

Page files primarily compose views. Data preparation is independently testable,
request/state ownership is preserved, and existing navigation, plots, tables,
exports, accessibility and refusal messages behave the same.

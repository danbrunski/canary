# W10 — Workbench areas, and a ROM Editor laid out like an editor

Requested 2026-10-27 by the operator: "On the ROM Editor, there are buttons for
all the different maps we can view/edit. Typically a UI would present these
like tabs or some file dropdown menu. … There's also a lot of stuff crammed
into this page - map edits, simulation, results, etc. These should probably be
broken out into either different pages or pages with tabs", then "the
navigation sidebar is getting pretty busy with a lot of items. Is there a
higher level of navigation we need to think about?"

Pure frontend except W10.7, which adds two fields to `rom_map.yaml` and its
schema. No truck, no Pi. Part A (W10.1–W10.6) lands and is committed before
Part B (W10.7–W10.13) starts, because Part B's layout assumes the collapsible
sidebar.

## Where it stands (2026-10-27, `dd41eb46`)

- **The sidebar lists every page at once.** `Workbench.tsx` renders all five
  `NAV` groups expanded: 39 pages (Observe 4, Engine Tuning 7, Diagnose 10,
  Model 6, System 12). Between 600 and 900 px the sidebar turns into one
  horizontal strip holding all 39.
- **The ROM Editor's table picker is a wrapping row of buttons.** Map 1 shows
  11 of them (`isViewableTable`), with names up to 62 characters ("Timing
  retard map 0xFFFF3630 (rpm x coolant, feeds 0xFFFF279A)"). The 2008 map
  defines 45 non-axis tables. The ~30 curves and scalars are not shown at all
  (only K, because it is editable), and every reverse-engineering pass adds
  more.
- **Everything is stacked on one page.** Intro, Open, a table of every
  library image (Recent ROMs), then Edit, Pending edits, Preview, Simulate and
  Simulation result, top to bottom. The Recent ROMs table is shown even when
  an image is loaded.
- **The header has no room for another row of controls.** Measured at 1280 px
  (Playwright, mock daemon): the header's children are 104, 104, 104, 104,
  283, 64 and 95 px wide, leaving about 290 px between the token pill and the
  connection badges. Five area buttons and a gear need about 300 px at the
  header's 0.78 rem type.

## Decisions (operator, 2026-10-27; do not re-open)

- **Areas are the five `NAV` groups**, with their labels unchanged (Observe,
  Engine Tuning, Diagnose, Model, System). The sidebar shows only the active
  area's pages. System sits at the right end of the area bar behind a ⚙,
  because its pages are settings visited rarely.
- **The area bar is a row at the top of the Workbench, not in the shell
  header.** The header has no room (above), and the Workbench already owns
  navigation (`DashboardShell`'s docstring). This also keeps the area bar
  visible when the sidebar is collapsed.
- **ROM editing and simulation stay in Engine Tuning.** The tuning context bar
  carries the selected log and the loaded ROM across Logs, Analysis, the ROM
  Editor and Flash, and the editor's coverage overlay reads the log chosen on
  Logs. Simulation is a property of the pending edits. If Simulate grows run
  history and comparison it may become its own Engine Tuning page; not here.
- **The sidebar is collapsible on every page**, remembered per browser. The
  ROM Editor's table tree, the page nav and a 16 x 16 grid side by side come
  to about 1300 px.
- **The ROM Editor gets three views: Tables | Edits & Preview | Simulate**,
  drawn with `SegmentedControl`, the one view-switch pattern (R08.11;
  `AnalysisPage` uses it for the same job).
- **The table picker is a grouped tree**, as in TunerPro, EcuFlash, WinOLS and
  HP Tuners. The grouping is data: a `group` field per table in
  `rom_map.yaml`, a closed set, never a list in the frontend.
- **The ROM choice becomes File-style:** a one-line header with **Open…** and
  **Recent ▾** once an image is loaded. The empty state keeps today's intro,
  Open box and Recent ROMs table.
- **The phone layout does not change.** It keeps its bottom bar and More sheet
  (P2.2). Below 900 px the table tree becomes a native `<select>` with one
  `<optgroup>` per group.

## Part A — Workbench areas

- [x] **W10.1 — Area memory in the nav model.** In `nav.ts`: `groupOf(item)`
  (the item's `NAV` group), and `itemForGroup(group, remembered)` returning the
  remembered page when it is still in that group, else `defaultItemForGroup`.
  A small hook (`hooks/useAreaMemory.ts`) keeps a `Record<NavGroupId,
  WorkbenchItem>` in `localStorage` (`canary.workbench.lastPage.v1`). It writes on
  every navigation, wraps every access in try/catch, and drops any stored id
  that `isWorkbenchItem` rejects or that belongs to another group (a page
  moved between groups must not strand the area).
  *Tests:* `nav.test.ts` or `useAreaMemory.test.ts`. The remembered page wins.
  A stale id, a page of another group, junk JSON and a throwing `localStorage`
  each fall back to the group's first page.
  *Done 2026-10-27:* `groupOf` and `itemForGroup` in `nav.ts`. `hooks/useAreaMemory.ts`
  (`readAreaMemory`, `useAreaMemory`) under `canary.workbench.lastPage.v1`, the
  key named like `canary.tuning.selectedLogId.v1`. It updates the memory while
  rendering, React's pattern for state derived from a prop.
  `hooks/useAreaMemory.test.ts` has 17 tests, junk values included.
- [x] **W10.2 — The area bar.** `components/AreaBar.tsx`: `<nav
  aria-label="Workbench areas">`, one button per `NAV` group in `NAV` order,
  the active one `aria-current="page"`. System is pushed to the right end
  (`ml-auto`) with a ⚙ glyph. Its accessible name stays "System" (the glyph
  `aria-hidden`). A click calls `onNavigate(itemForGroup(group, memory))`.
  Rendered by `Workbench` above the sidebar and content, desktop and 600–900 px
  only (the phone has its bottom bar). Style the buttons as tabs (an underline
  or filled state on the active one), not as another set of pills: the header
  already has five.
  *Tests:* `AreaBar.test.tsx`. Five buttons in `NAV` order, the active area
  marked, a click navigates to the remembered page, System is last.
  *Done 2026-10-27:* `components/AreaBar.tsx`, tabs with an accent underline
  (`areaTabClass`). The far-end group is a `settings` flag on `NavGroupDef`
  rather than a hard-coded id. `components/AreaBar.test.tsx` has 4 tests.
- [x] **W10.3 — The sidebar shows one area.** `Workbench` renders only
  `groupOf(active)`'s items. The nav keeps `aria-label="Workbench sections"`.
  The in-sidebar group heading is dropped because the area bar names the area.
  The 600–900 px horizontal strip then holds at most 12 pages. The sidebar and
  the content pane stay adjacent siblings: `e2e/dashboard.spec.ts`'s overflow
  test reads the pane as `nav.nextElementSibling`.
  *Done 2026-10-27:* `Workbench` lists `NAV`'s group for `groupOf(active)`
  inside `workbenchBodyClass`'s grid, below the bar. `navGroupLabelClass` is
  deleted. **Found on the way, and older than W10:** between 600 and 900 px
  the strip had always been invisible on a tall page. Its grid row measured
  17 px, padding only, because a scroll container in an `auto` row may shrink
  to nothing, and the pages spilled under the panel. This was measured on a
  build of `dd41eb46` at 800 x 500 too. The fix is a `max-content` row at
  narrow widths. e2e `dashboard.spec.ts` "between 600 and 900px the sidebar
  strip keeps its pages visible" fails with the fix removed and passes with it.
- [x] **W10.4 — Collapse the sidebar.** A toggle at the sidebar's foot
  (`aria-expanded`, labels "Collapse sidebar" / "Expand sidebar", « / »).
  Collapsed, the sidebar is a ~2.5 rem rail holding only the expand button,
  and the content pane takes the width. The state is persisted in
  `localStorage` (`canary.workbench.sidebarCollapsed.v1`, try/catch, default
  expanded). The toggle is hidden below 900 px, where the sidebar is a strip.
  *Tests:* the toggle hides the page buttons and shows them again, the state
  survives a remount, a throwing `localStorage` leaves it expanded.
  *Done 2026-10-27:* `useSidebarCollapsed` (`canary.workbench.sidebarCollapsed.v1`;
  only a stored `true` collapses). `useIsNarrow` (`NARROW_MEDIA_QUERY`) keeps
  the strip uncollapsed below 900 px. `Workbench.test.ts` "Workbench sidebar
  collapse (W10.4)" has 3 tests, and the e2e area-bar test collapses and
  expands in Chromium.
- [x] **W10.5 — Sweep the tests.** `Workbench.test.ts`: "renders all five
  group labels" reads them from the area bar. "places Capture Profiles under
  System" renders a System page. "switches on navigate" navigates within the
  active area. New: a page of another area is not in the sidebar. Clicking an
  area lands on its remembered page. `e2e/dashboard.spec.ts`: the
  workbench-landing test looks for the group labels in the areas nav and
  switches area by click. The overflow test still passes. `phone.spec.ts`: the
  area bar is absent on a phone. `RomLibraryPage.test.tsx` and
  `romEditorOverlay.spec.ts` press sidebar buttons of Engine Tuning pages,
  which remain.
  *Done 2026-10-27:* `Workbench.test.ts` has 20 tests. It also stubs `fetch`
  for the file, so a navigation test no longer sends real requests. That
  removes the `act()` warning `DtcPanel` printed from the phone strip test.
  `e2e/dashboard.spec.ts` gained the area-bar test and the strip regression,
  and `phone.spec.ts` asserts there is no area bar. Gate on 2026-10-27:
  vitest 1305 / 1305 (122 files), e2e 43 passed and 3 skipped (both projects),
  `pnpm typecheck`, `pnpm lint` and `pnpm format:check` clean.
- [x] **W10.6 — Docs.** The `Workbench` and `DashboardShell` docstrings say
  areas + per-area sidebar. A status entry in `tasks/README.md`. Grep `docs/`
  for "sidebar" outside `archive/` and `analyses/`. Archived W01 is history and
  stays as written.
  *Done 2026-10-27:* the `Workbench` and `DashboardShell` docstrings, and the
  `tasks/README.md` status entry. `grep -rni sidebar docs` outside
  `archive/` and `analyses/` finds only that new entry, so no live doc
  described the old sidebar.

**Gate for Part A:** `pnpm typecheck`, `pnpm lint`, `pnpm format:check`,
`pnpm test` (the whole suite: navigation touches every page's tests) and
`pnpm e2e` in `src/dashboard/static`, then `scripts/push.sh`.

## Part B — the ROM Editor

- [ ] **W10.7 — `group` and `short_name` on ROM tables.** `src/analysis/rom_map.py`
  `Table` gains:
  - `group: TableGroup | None`, a closed `Literal` (`fuel`, `ignition`,
    `airflow`, `emissions`; extend the set when a table needs it). A `RomMap`
    validator refuses a table that no other table references as an axis and
    has no `group`, naming the table. Axis tables do not need one; they are
    never listed.
  - `short_name: str | None`, at most 32 characters, the tree's label. It
    defaults to `name`. A validator refuses two tables that would show the same
    tree label in one slot. This covers a slot's own copy plus every
    single-copy table: "Fuel Targets" (Map 1) beside "Fuel Targets
    (calibration copy)" must stay distinguishable, and so must the two "Fill
    time constant" maps.

  `rom_image.py` serves both fields wherever it serves `label` (the three
  payload builders: `table_definitions`, `table_values` and the tables
  payload), and `types/romLibrary.ts` `RomTableValues` gains them. Fill both
  `data/nissan-xterra-2008/rom_map.yaml` (45 non-axis tables) and
  `data/nissan-xterra-2013/rom_map.yaml` (6). Give every name over 32
  characters a `short_name` that keeps what tells it apart. Where no plain
  English name is established, the address that identifies it stays in the
  label ("Retard 0xFFFF3630 by coolant"); do not invent function names the
  evidence does not support.
  *Tests:* `tests/test_rom_map.py` (missing group refused and named,
  colliding tree labels refused and named, an axis needs no group, both
  vehicle maps load), `tests/test_rom_image.py` or the route test for the
  served fields. `docs/headless-api.md`'s `/tables` entry names the two
  fields.
- [ ] **W10.8 — The ROM header.** With an image loaded, the `ROM Editor`
  panel becomes one line: name, the first 12 hex digits of the sha,
  `RevisionChip`, `SimulateChip`, then **Open…** and **Recent ▾** on the right.
  The header is still the `OpenRom` drop target, so a dropped file still
  opens. **Recent ▾** opens a popover listing the library newest first (name,
  revision and simulate chips). Choosing one loads it and closes the popover,
  as do Escape and an outside click. Busy, refusal, "already in the library"
  and the missing-link notice keep their wording, under the header. With
  nothing loaded, the page is today's: intro, Open box, Recent ROMs table. The
  intro paragraph appears only in that state.
  *Tests:* in `RomEditorPage.test.tsx`. Loaded, the Recent ROMs table is gone
  and the popover lists every entry. Choosing from it switches the image and
  writes `?rom=`. A drop on the header opens the file. The empty state is
  unchanged.
- [ ] **W10.9 — Three views.** Under the header, a `SegmentedControl`
  (`label="ROM Editor views"`): **Tables**, **Edits & Preview** and
  **Simulate**.
  - **Tables:** the map-slot choice, the tree (W10.10), and for the chosen
    table its decode, axes, notes, live-overlay toggle, grid and operation
    panel.
  - **Edits & Preview:** Pending edits, then Preview.
  - **Simulate:** the form, run state and result.

  The labels carry state, as plain text inside the label: "Edits & Preview
  (3)" with the pending count; "Simulate · running" while a run is in flight,
  "· stale" when the shown result's edits differ from the pending ones (the
  existing `stale` comparison), and "· failed". The view rides in the URL as
  `?view=tables|edits|simulate` beside `?rom=` (`replaceState`). A missing or
  unknown value means Tables.

  **Inactive views stay mounted and `hidden`.** `SimForm` holds about ten
  pieces of local state (typed fields, typical-value marks, edited edges),
  and `OperationPanel` its pending value, all lost on unmount. The one
  exception is `LiveOverlay`, which mounts only while Tables is showing: its
  250 ms clock would otherwise re-render a hidden grid four times a second.
  Applying an edit stays on Tables; the count in the label is the feedback.

  *Tests:* a SimForm field typed, a view switch and back, the value intact.
  The labels' counts and states. `?view=` restores the view on load, and
  junk falls back to Tables. A run started on Simulate and left while running
  still lands its result (state is the Workspace's, not the view's).
- [ ] **W10.10 — The table tree.** `romEditor/TableTree.tsx`, the left column
  of Tables (about 15 rem; the grid takes the rest):
  - The Map slot `<select>` at the top when the image has more than one slot.
  - A filter box: a case-insensitive substring over `short_name` and `name`.
    It hides empty groups, and says "No table matches" when nothing is left.
  - Groups in the schema's order, each with a heading button that collapses
    it (all expanded by default). A filter match overrides a collapsed group.
  - One button per table: the tree label, the full `label` as `title`, an ✎
    marker on editable tables, a pending-edit count when edits touch the
    table, and a faint marker with the confidence in its accessible name when
    the decode is `inferred` or `unknown`.
  - `<nav aria-label="Tables">`, the chosen table `aria-current="true"`.

  Choosing a table keeps today's side effects (clear the selection and the
  operation refusal). The detail column's heading is the table's full `label`.
  The intro line "Click a cell, shift-click…" and the viewing-only note stay
  with the grid. Delete the old `role="group" aria-label="Table"` button row.
  *Tests:* `romEditor/TableTree.test.tsx` covers grouping and order, the
  filter, collapse, the ✎ / pending / confidence markers and `aria-current`.
  `RomEditorTables.test.tsx` covers choosing from the tree, which changes the
  grid.
- [ ] **W10.11 — Narrow widths.** Below 900 px the tree is a native
  `<select aria-label="Table">` with one `<optgroup>` per group (the editable
  and pending markers as text in the option), the filter hidden, the grid full
  width. The phone's ROM tab reaches this page (`PHONE_TABS`), so it is used.
  *Tests:* an e2e check at 412 px: the select is present, choosing an option
  changes the grid, and `overflowReport` finds no horizontal page overflow.
- [ ] **W10.12 — Sweep the ROM Editor tests.** `RomEditorPage.test.tsx`,
  `RomEditorTables.test.tsx`, `RomEditorSimulate.test.tsx`,
  `RomEditorOverlay.test.tsx`, `romEditor/CellGrid.test.tsx`,
  `e2e/romEditor.spec.ts` and `e2e/romEditorOverlay.spec.ts` find tables
  through the `Table` button group and the sections through panel headings.
  Move them to the tree / select and the views. Keep every behavioural
  assertion: re-point each, don't delete it. The e2e short-viewport
  regression ("one click selected 7-11 cells") stays and runs inside the
  Tables view.
- [ ] **W10.13 — Docs.** `RomEditorPage`'s docstring (views, tree, header).
  W09's text describes behaviour, not layout; check its "the page shows"
  sentences and correct any that no longer hold. Add the `tasks/README.md`
  status entry, and record the closure here.

**Gate for Part B:** as Part A, plus `.venv/bin/python -m pytest
tests/test_rom_map.py tests/test_rom_image.py` (and whatever the route tests
are named) for W10.7, then `scripts/push.sh`.

## Not in scope

- **Curves and scalars in the tree** (the ~30 tables `isViewableTable` drops
  today) are left out until the operator decides; asked 2026-10-27, no answer
  yet. A one-row grid or the existing `Curve` chart would both fit the tree
  without changing it.
- Simulate as its own page (see Decisions), a header redesign, and regrouping
  Diagnose's 10 pages.

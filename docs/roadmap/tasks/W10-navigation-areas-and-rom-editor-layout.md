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
- **Every non-axis table is listed, curves and scalars included** (operator,
  2026-10-27: "show hidden items in the new table list"). `isViewableTable`
  becomes "not an axis". A curve is a one-row or one-column grid and a scalar
  a single cell, the way K is shown today.
- **Short names keep the address where no plain-English name is
  established** (operator, 2026-10-27), e.g. "Timing retard map 0xFFFF3630".
  No function names that the evidence does not support.
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

- [x] **W10.7 — `group` and `short_name` on ROM tables.** `src/analysis/rom_map.py`
  `Table` gains:
  - `group: TableGroup | None`, a closed `Literal` (`fuel`, `ignition`,
    `airflow`, `emissions`; extend the set when a table needs it). A `RomMap`
    validator refuses a table that no other table references as an axis and
    has no `group`, naming the table. Axis tables do not need one; they are
    never listed.
  - `short_name: str | None`, the tree's label, which defaults to `name`. A
    tree label is at most 36 characters (about two lines of the 15 rem tree),
    so a table whose `name` is longer must declare a `short_name`. The
    validator refuses the omission and names the table. A validator also
    refuses two tables that would show the same tree label in one slot. This covers a slot's own copy plus every
    single-copy table: "Fuel Targets" (Map 1) beside "Fuel Targets
    (calibration copy)" must stay distinguishable, and so must the two "Fill
    time constant" maps.

  `rom_image.py` serves both fields wherever it serves `label` (the three
  payload builders: `table_definitions`, `table_values` and the tables
  payload), and `types/romLibrary.ts` `RomTableValues` gains them. Fill both
  `data/nissan-xterra-2008/rom_map.yaml` (45 non-axis tables) and
  `data/nissan-xterra-2013/rom_map.yaml` (6). Give every name over 36
  characters a `short_name` that keeps what tells it apart. Where no plain
  English name is established, the address that identifies it stays in the
  label ("Retard 0xFFFF3630 by coolant"); do not invent function names the
  evidence does not support.
  *Tests:* `tests/test_rom_map.py` (missing group refused and named,
  colliding tree labels refused and named, an axis needs no group, both
  vehicle maps load), `tests/test_rom_image.py` or the route test for the
  served fields. `docs/headless-api.md`'s `/tables` entry names the two
  fields.
  *Done 2026-10-27:* `TableGroup` (a closed `Literal`) and `TREE_LABEL_MAX = 36`
  in `rom_map.py`; `Table.group`, `Table.short_name` (1–36 characters) and the
  `tree_label` property. `RomMap._check_tree_labels` refuses, naming the table,
  a listed table without a group, a name over 36 characters without a short
  name, and two tables sharing a tree label in one slot (the slot's copies plus
  every single-copy table). `RomMap.axis_table_ids()` is now the one definition
  of an axis, and `rom_image._axis_ids` uses it. `table_definitions` and
  `table_values` serve both fields, so `/tables` carries them. The 2008 map
  lists 45 tables (fuel 9, ignition 27, airflow 8, emissions 1), with 28 short
  names and a longest label of 35 characters. The address stays wherever no
  function is established ("Timing retard map 0xFFFF3630"). The 2013 map has
  6 tables and needed groups only. `tests/test_rom_map.py` gains 8 tests and
  `tests/test_rom_image.py` gains 1; both files pass (93 tests), and so does
  every test file that loads a ROM map except the 4 data-dependent slow-tier
  failures below. `docs/headless-api.md` names the two fields.
- [x] **W10.8 — The ROM header.** With an image loaded, the `ROM Editor`
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
  *Done 2026-10-27:* `OpenRom` gains a `bare` variant for the loaded header: no
  dashed box until a file is dragged over, `children` first, then the button
  and `trailing` at the far end. It is still the drop target ("accepts a
  dropped file" drops on it). `romEditor/RecentMenu.tsx` is **Recent ▾**, a
  `dialog` "Recent ROMs" that holds the same `BasePicker` table. A choice,
  Escape, a click elsewhere or a second press closes it. The intro and the
  Recent ROMs table show only while nothing is loaded. The button keeps its
  existing label, "Open another ROM…", rather than "Open…". Panels clip their
  content (`overflow-hidden`), so the header panel is `overflow-visible!`. The
  e2e test checks that `elementFromPoint` at a menu row's centre hits that
  row's button, and it fails without the fix: a plain Playwright click did not
  notice the clipping. Tests: `RomEditorPage.test.tsx` "the loaded header
  (W10.8)" (2) and the e2e "loaded, the header is one line…".
- [x] **W10.9 — Three views.** Under the header, a `SegmentedControl`
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
  *Done 2026-10-27:* `SegmentedControl label="ROM Editor views"` in `Workspace`.
  The labels are "Edits & Preview (n)" and "Simulate · running", "· failed" or
  "· stale" (`simStale`, the result panel's own comparison). `viewFromUrl` and
  `writeViewToUrl` keep `?view=` beside `?rom=`. Each view is a `hidden`
  wrapper, so all three stay mounted, and `LiveOverlay` renders only while
  `view === "tables"`. Tests: `RomEditorPage.test.tsx` "three views (W10.9)"
  (3: order and URL, restore with junk falling back, a half-made operation kept
  in the same form element). `RomEditorSimulate.test.tsx` "Simulate as a view
  of its own (W10.9)" (3: a typed field kept, the running label and a run left
  mid-flight still landing, the failed label), plus "· stale" in "notes when
  the pending edits change after a run". `RomEditorOverlay.test.tsx` "is
  mounted only while Tables is showing", which fails when the `view` condition
  is removed.
- [x] **W10.10 — The table tree.** `romEditor/TableTree.tsx`, the left column
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
  - Every non-axis table, curves and scalars included (Decisions). The
    `group` comes from the daemon. A table without one, sent by a daemon older
    than W10.7, is listed under "Other" at the end, so the tree still works
    against it.

  Choosing a table keeps today's side effects (clear the selection and the
  operation refusal). The detail column's heading is the table's full `label`.
  The intro line "Click a cell, shift-click…" and the viewing-only note stay
  with the grid. Delete the old `role="group" aria-label="Table"` button row.
  *Tests:* `romEditor/TableTree.test.tsx` covers grouping and order, the
  filter, collapse, the ✎ / pending / confidence markers and `aria-current`.
  `RomEditorTables.test.tsx` covers choosing from the tree, which changes the
  grid.
  *Done 2026-10-27:* `romEditor/TableTree.tsx`, using `treeLabel`,
  `TREE_GROUPS`, `groupTables`, `matchesTableFilter` and `pendingEditCount`
  from `lib/romEditor.ts`. `isViewableTable` is now "not an axis". The Tables
  view is a 15 rem column for the tree and the rest for the table, headed by
  the table's full label (an `h3`). One deviation from the text above: the
  confidence, like the ✎ and pending marks, goes in the entry's `title` (its
  accessible description), not its name. Each entry is named by its tree label
  alone ("Fuel Compensation"), and the marks themselves are `aria-hidden`. The
  old `Table` button row is gone. Tests: `romEditor/TableTree.test.tsx` (9),
  and `RomEditorTables.test.tsx` rewritten for the tree (grouped order;
  choosing changes the grid and the heading; curves and scalars listed and
  drawn).
- [x] **W10.11 — Narrow widths.** Below 900 px the tree is a native
  `<select aria-label="Table">` with one `<optgroup>` per group (the editable
  and pending markers as text in the option), the filter hidden, the grid full
  width. The phone's ROM tab reaches this page (`PHONE_TABS`), so it is used.
  *Tests:* an e2e check at 412 px: the select is present, choosing an option
  changes the grid, and `overflowReport` finds no horizontal page overflow.
  *Done 2026-10-27:* `TableTree` with `narrow` (`useIsNarrow`, below 900 px)
  renders `<select aria-label="Table">` with one `optgroup` per group. "✎" and
  "(n pending)" go in the option text, and the filter is absent. The Tables
  grid is a single column there. Tests: `TableTree.test.tsx` "below 900px",
  and the e2e at 412 × 915 "the table tree is a select…" (the select is there,
  a choice changes the grid, `overflowReport` is clean). The existing phone e2e
  now picks its table from the select (`chooseTable`).
- [x] **W10.12 — Sweep the ROM Editor tests.** `RomEditorPage.test.tsx`,
  `RomEditorTables.test.tsx`, `RomEditorSimulate.test.tsx`,
  `RomEditorOverlay.test.tsx`, `romEditor/CellGrid.test.tsx`,
  `e2e/romEditor.spec.ts` and `e2e/romEditorOverlay.spec.ts` find tables
  through the `Table` button group and the sections through panel headings.
  Move them to the tree / select and the views. Keep every behavioural
  assertion: re-point each, don't delete it. The e2e short-viewport
  regression ("one click selected 7-11 cells") stays and runs inside the
  Tables view.
  *Done 2026-10-27:* `src/test/romEditorViews.ts` (`showRomView`, `treeTable` and
  `findTreeTable` by full label, `treeContents`, `recentEntry`,
  `chooseRecent`) is shared by the four page test files. The e2e spec has
  `treeTable`, `chooseTable`, `viewButton` and `showView`. Every assertion was
  re-pointed and none dropped. Two were rewritten because their premise
  changed. "Never offers … a one-dimensional curve" is now "lists every table
  but the axes", the operator's decision. "Levers above the Simulate region"
  is now "levers on Edits & Preview, ahead of Simulate in the document, with
  the Simulate form hidden". The short-viewport single-click check runs inside
  Tables. `CellGrid.test.tsx` and `e2e/romEditorOverlay.spec.ts` needed no
  change. The fixtures carry `group`. Gate: `pnpm typecheck`, `lint` and
  `format:check` clean; `pnpm test` 123 files, 1323 tests; `pnpm e2e` 45
  passed and 3 skipped (the existing phone-only and desktop-only skips).
- [x] **W10.13 — Docs.** `RomEditorPage`'s docstring (views, tree, header).
  W09's text describes behaviour, not layout; check its "the page shows"
  sentences and correct any that no longer hold. Add the `tasks/README.md`
  status entry, and record the closure here.
  *Done 2026-10-27:* `RomEditorPage`'s docstring covers the header, the views,
  the tree, `?view=`, the mounted views and the overlay. W09.8 and W09.12 each
  carry a one-line note where W10 changed what they describe. The
  `tasks/README.md` entry says Part B landed.

**Gate for Part B:** as Part A, plus `.venv/bin/python -m pytest
tests/test_rom_map.py tests/test_rom_image.py` (and whatever the route tests
are named) for W10.7, then `scripts/push.sh`.

**Closed 2026-10-27.** Part A landed in `495d3143`, Part B in the commit after
it. The slow tier still fails 4 tests in
`tests/test_plant_validation_ecu_outputs.py` (`assert 64 == 33` on BFS post
reads). They read local session data, failed the same way with the W10.7
changes stashed, and are not this work's.

## Part C — the tree knows which tables have map slots (operator, 2026-10-27)

Asked after Part B: "there are a lot more maps in the rom editor than uprev
would offer … not everything has 5 slots", "instead of having a map slot
selector at the top, we just show Fuel maps 1 - 5 in the tree", "and maybe
have a filter for editable fields and view only".

Where Part B left it: of the 2008 map's 45 listed tables, only six families
live in UpRev's map slots (Fuel Targets, Fuel Compensation, trims A and B, K,
and the Load Point Scaler's slot copy). The other 35 have one copy in the
ROM, yet the Map slot select sat above all of them, so "Map 3" listed 35
tables that are not in Map 3. Only five tables take an edit. A stock image
(the 2013's) has no slots at all; UpRev adds them when it flashes.

- [x] **W10.14 — Map slots in the tree, no slot select.** The tree takes every
  listed table of every slot. Slotted copies that share a tree label are one
  family: a disclosure row ("Fuel Targets, 5 maps", `aria-expanded`) over a
  `role="group"` named by the family, holding one entry per slot ("Map 1"…,
  titled with the copy's full label). A single-copy table is one entry with
  no slot, as before. A family whose filter leaves one copy shows that copy
  as a plain entry. The chosen table's family starts open; the rest start
  closed; a text filter opens every match. The family row carries ✎ when a
  copy is editable and the pending count summed over its copies. The Map
  slot select and `Workspace`'s slot state go; the slot is the chosen
  table's own. The default table is the first listed one that is Map 1 or
  single-copy. Below 900 px a slotted copy is its own option ("Fuel Targets ·
  Map 2"). The model is `lib/romEditor.ts` `treeGroups`, replacing
  `groupTables`.
  *Tests:* `TableTree.test.tsx` (families, a single copy left by a filter, the
  open rules, the ✎ and summed pending marks, the narrow options);
  `RomEditorTables.test.tsx` and `RomEditorOverlay.test.tsx` re-pointed from
  the slot select to the families; an image without slots lists plain
  entries.
  *Done 2026-10-27:* `treeGroups` folds copies by tree label where the first
  copy stands and sorts them by slot; `familyKey` names the family. The family
  row is `data-tree="family"`, named "Fuel Targets, 5 maps", titled
  "Fuel Targets · 5 maps". The chosen table's family also opens when the choice
  arrives from elsewhere (narrow select, then a wider window), and the rest
  keep their state. A slotted table with one copy (trim A, the base timing map
  in the fixtures) is a plain entry, and below 900 px it carries no "· Map N"
  either. `Workspace` lost `slotChoice`, `slots` and `inSlot`. The default
  table is the first Map 1 or slotless one. The Tables panel dropped its count:
  copies are not tables. `pendingEditCount` still matches by id alone, so a
  pending edit on a Maps 2-5 id would count on every copy sharing it. Only
  Map 1 takes an edit today, so it cannot happen yet. It matters once
  editing reaches other slots.
- [x] **W10.15 — Editable / View only / All.** A `SegmentedControl` ("Show
  tables") above the filter box: All, Editable (`isEditableTable`), View only
  (the rest). It applies with the text filter, below 900 px too, and is
  remembered per browser (`canary.romEditor.tableKinds.v1`; junk reads as
  All). An empty tree names why: "No table matches “x”." for text, else "No
  editable table in this image." / "No view-only table in this image.". The
  chosen table stays chosen when the filter hides it.
  *Tests:* `TableTree.test.tsx` (each kind, combined with text, the empty
  sentences, the remembered choice and junk).
  *Done 2026-10-27:* `TABLE_KINDS` / `TableKinds` in `lib/romEditor.ts`;
  `TABLE_KINDS_STORAGE_KEY` in `TableTree.tsx` through `useLocalStorageJson`,
  read back through `readKinds`. Below 900 px, a choice the kind hides stays
  the select's value as an extra option ahead of the groups. Full-size
  buttons wrapped onto two rows in the 15rem column, so `SegmentedControl`
  gained `compact` (small buttons, sized by `!` because `cn` does not merge).
- [x] **W10.16 — Sweep, e2e and docs.** `romEditorViews.ts`' `treeTable` opens
  a closed family before looking. The e2e helpers pick a slot copy through
  its family, the overlay spec's slot navigation goes through the tree, and
  the 412 px test picks "Fuel Compensation · Map 1 ✎". `RomEditorPage`'s
  docstring, this file and `tasks/README.md`.
  *Done 2026-10-27:* `treeTable` opens closed families one at a time and
  closes any it opened that did not hold the copy, so `treeContents` still
  reads what the operator would see. `treeContents` leaves out the Show
  tables buttons. The e2e spec gained `treeCopy(page, family, slot)`, and
  `chooseTable` takes an optional slot. The overlay spec reaches Map 2 through
  the open Fuel Targets family and returns at 430 px through "Fuel Targets ·
  Map 1 ✎". Every slot-select assertion was re-pointed and none dropped.

**Gate for Part C:** as Part B's frontend gate (no Python change), then
`scripts/push.sh`.

**Part C closed 2026-10-27.** Gate: `pnpm typecheck`, ESLint on the changed
files and `pnpm format:check` clean; `pnpm test` 123 files, 1334 tests, no
act() warnings; `pnpm e2e` 45 passed and 3 skipped (the existing phone-only
and desktop-only skips). `docs/offline-rom-targets.md` described the slot
dropdown and now describes the families and Show tables; W09.8 carries a
one-line note.

**Not in Part C:** which copy is editable on an image without slots. Today
only Map 1 copies take an edit, so a stock 2013 image is view only; making
its single copy editable touches `rom_editor.py` and the simulator's Map 1
assumption, and needs K mapped for the 2013. That is its own item.

## Not in scope

- **A curve drawn as a chart** (the existing `Curve` component) rather than a
  one-row grid. The grid is enough to read and compare values, and it keeps
  one rendering for every table.
- Simulate as its own page (see Decisions), a header redesign, and regrouping
  Diagnose's 10 pages.

# W — workbench navigation and Engine Tuning pages

Implementation checklists for [`../workbench-workflows.md`](../../workbench-workflows.md),
which holds the reasoning. Read that first; these files only say what to build,
in what order, and how to know it is done.

Written 2026-09-21. Same conventions as the D-series: **task-level truth is the
checkboxes in these files**, `[x]` only with the evidence linked (test name,
commit, screenshot, or measurement), `[~]` for partial with the remainder named.

| file | covers | roadmap task | needs the truck |
|---|---|---|---|
| [W01](W01-navigation-regroup.md) ✅ | five groups, renames, redirects, reserved entries | W1, W10 | no |
| [W02](W02-tuning-context-bar.md) ✅ | shared context bar, calibration id in `/status`, readiness chips | W2 | one KOEO probe |
| [W03](W03-logging-page.md) ✅ | Logging page, ended-capture announcement | W3, W4 | final check only |
| [W04](W04-logs-and-analysis.md) ✅ | Logs (table / visual) and Analysis pages | W5, W6 | no |
| [W05](W05-rom-maps-trace-coverage.md) ✅ | ECM-coordinate trace, coverage overlay | W7, W8 | final check only |
| [W06](W06-calibration-page.md) ✅ | flash history, memory-channel state, re-verify | W9 | final check only |
| [W07](W07-flash-interlocks.md) ✅ | the two AGENTS.md conditions as server-side code | W11 | no |
| [W08](W08-rom-library.md) ✅ | ROM library, memory map, compare, ECU backup (read-only) | operator request 2026-10-04 | W08.9 only (KOEO) |

✅ complete and moved to [`archive/`](./). As of 2026-10-13 every file
here is archived (W05 closed with `6430d7f3`, W08 with its KOEO backup), and
this index moved to `archive/` with them.

## Order

W01 first: every other file adds pages to the group it creates. W02 next,
because W03–W06 all render inside its context bar. After that W03–W06 are
independent of each other and can land in any order or in parallel. **W07 has
no dependency on any of them and must land before anything that can program a
module exists** — it is listed last only because nothing here builds that
capability.

## Standing rules for all of these

- **Gate:** `pnpm test` and `pnpm typecheck` in `src/dashboard/static` for any
  frontend change; the whole-repo Python gate for any backend change.
- **Pages stand alone.** No page may assume another was visited. Test each page
  by loading its route directly (`#wb/<item>`) with empty group state.
- **An empty state names its cause and offers its fix.** Every page below lists
  its empty states explicitly; each is a test case, not a nicety.
- **No analysis on the Pi while it collects.** Any endpoint that reads a whole
  session must go through the bounded single-flight child the digest already
  uses, and must refuse or defer while a capture is active rather than compete
  with the collector for memory. A nice-19 pandas run once cost a 3-minute
  outage and 150k dropped records.
- **Another session may have `Workbench.tsx`, `nav.ts` and
  `types/navigation.ts` open.** They were dirty on 2026-09-21. Check
  `git status` before W01 and coordinate rather than overwrite.
- **Additive API changes only.** New `/status` keys and new endpoints; never a
  renamed or removed field, since the companion app and the MCP server read the
  same payloads.

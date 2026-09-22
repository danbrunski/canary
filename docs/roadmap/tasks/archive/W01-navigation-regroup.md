# W01 — regroup the navigation

Roadmap tasks W1 and W10. Pure frontend; no backend change; no truck.

The navigation is a typed union plus a `switch`, so the compiler finds every
site that needs touching — lean on that rather than on grep.

**Files:** `src/dashboard/static/src/types/navigation.ts` (`NavGroupId`,
`WorkbenchItem`, `NavItemDef`), `nav.ts` (`NAV`, `findItem`, `groupOf`,
`defaultItemForGroup`, `isWorkbenchItem`, `DEFAULT_ITEM`),
`hooks/useHashRoute.ts` (`parseHash` / `toHash`), `panels/Workbench.tsx`
(`renderItem`).

## Target

| group id | label | items (new id ← old id) |
|---|---|---|
| `observe` | Observe | `observe-charts` ← `live-charts`, `observe-trip` ← `live-trip`, `observe-alarms` ← `live-alarms`, `observe-builder` ← `live-builder` |
| `tuning` | Engine Tuning | `tuning-logging` (new), `tuning-logs` (new), `tuning-analysis` (new), `tuning-rom-maps` ← `live-rom-maps`, `tuning-calibration` (new), `tuning-rom-editor` (planned), `tuning-flash` (planned) |
| `diagnose` | Diagnose | `diag-anomalies`, `diag-dtc`, `diag-monitors`, `diag-trends`, `diag-tpms`, `diag-llm-assist` (ids unchanged), `diag-explore` |
| `model` | Model | `models-overview`, `models-behavior`, `models-training`, `model-thresholds` ← `models-tuning`, `models-health`, `model-dataset-health` ← `data-health` |
| `system` | System | `system-data-sources` ← `diag-data`, `system-sessions` ← `data-sessions`, `system-export` ← `data-export`, `system-custom-pids` ← `data-custom-pids`, `system-profiles` ← `control-profiles`, `system-capture-profiles` ← `control-capture`, `system-power` ← `control-power`, `system-control` ← `control-main`, `system-test` ← `control-test`, `system-info` ← `diag-system` |

## Tasks

- [x] **W01.1 — Route aliases before anything moves.** Add an `ITEM_ALIASES:
  Record<string, WorkbenchItem>` map in `nav.ts` and have `parseHash` resolve
  through it, so `#wb/live-rom-maps` and every other old id keeps working.
  `docs/offline-rom-targets.md` and the operator's bookmarks point at old ids.
  *Test:* `useHashRoute.test.ts` — every id in the pre-change `WorkbenchItem`
  union resolves to a valid item; an unknown id still falls back to
  `DEFAULT_ITEM`.
- [x] **W01.2 — Change the unions.** `NavGroupId` and `WorkbenchItem` per the
  table. Let `tsc` list the breakage; fix `renderItem` and every `onNavigate`
  call site it reports. Do not leave a `default:` arm in `renderItem` — the
  exhaustiveness error is the safety net for the next page added.
- [x] **W01.3 — `NAV` per the table**, groups in the order Observe, Engine
  Tuning, Diagnose, Model, System. `DEFAULT_ITEM` stays the charts page under
  its new id.
- [x] **W01.4 — Rename the detector page.** Label `Alert Thresholds`; the panel
  component stays `TuningPanel.tsx` for now (renaming the file is churn with no
  user-visible effect) but its heading and any in-page copy that says "Tuning"
  change. *Test:* no rendered Model-group page contains the bare word "Tuning".
- [x] **W01.5 — Planned entries.** Add `planned?: boolean` beside the existing
  `placeholder?` on `NavItemDef`. A planned item renders in the nav, visibly
  muted, and its page is a short statement of what will live there and what it
  is waiting on — for `tuning-flash`, that is W07. It is not a placeholder for
  a finished backend, which is what `placeholder` already means; keep the two
  distinct.
- [x] **W01.6 — New pages render a named empty shell** (`tuning-logging`,
  `tuning-logs`, `tuning-analysis`, `tuning-calibration`) so W02–W06 each land
  as a fill-in rather than a nav change.
- [x] **W01.7 — Update every doc that names a route.** `grep -rn "#wb/" docs/`
  and `docs/offline-rom-targets.md` in particular. Aliases keep old links
  alive; the docs should still say the new ones.

## Done when

`pnpm test` and `pnpm typecheck` pass; every old `#wb/<id>` lands on the right
page; the five groups render in order; no page is reachable only through
another page.

Completed 2026-09-21. Evidence: `useHashRoute.test.ts` exercises all 28 old
routes and the unknown-route fallback; `Workbench.test.ts` covers group order,
planned entries, named shells, and the Alert Thresholds rename. `pnpm test`
(278 tests) and `pnpm typecheck` pass.

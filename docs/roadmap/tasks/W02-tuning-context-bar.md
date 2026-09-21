# W02 — the Engine Tuning context bar

Roadmap task W2. Depends on W01. One short KOEO probe on the truck (W02.1);
everything else is desk work.

```text
2008 Xterra · running ZS44A-20260920 · capture: rom-trace 00:12:41 · log: <selected> · ROM: <loaded>
```

This strip is what lets the Engine Tuning pages be visited in any order: they
share a subject without sharing a sequence.

**Files:** new `panels/tuning/TuningContextBar.tsx` and
`hooks/useTuningContext.ts`; `panels/Workbench.tsx` (render the bar above any
`tuning-*` item); backend `src/dashboard/status.py` (`build_status`),
`src/collection/tuning_profile.py` (`snapshot()` already carries
`running_tune_revision` and `memory_withheld`).

## Measure before designing

- [ ] **W02.1 — Does `21 10` answer outside session `0xC0`?** The calibration
  id read (`0x21 10` → ASCII `1ZS44A00000`) was only ever sent inside the
  Mode-22 session. Whether it also answers in the default session decides the
  whole design of W02.2: if it does, the daemon can read it at start-up and on
  every reconnect for free; if not, it has to ride the DID window. One KOEO
  probe with `tools/probe_local_identifier.py`, engine off, no session entered.
  Record the result in `data/dids.yaml` beside the `access` block.

## Backend

- [ ] **W02.2 — Read the calibration id and surface it.** On connect and after
  any reconnect, read `21 10` once (per W02.1's answer) and cache it. Add to
  `/status`: `ecu_calibration_id` (raw ASCII), `ecu_calibration_read_at`, and
  `calibration_matches_registry` — true when the id is consistent with the
  newest entry in `tune_revisions.yaml`, false when not, null when either side
  is unknown. The registry keys revisions by name (`ZS44A-20260920`) and the
  ECU reports a part-number-style id (`1ZS44A00000`), so this needs a
  `calibration_id` field added to `TuneRevision`; do **not** try to derive one
  from the other by string matching. *Test:* null / match / mismatch.
- [ ] **W02.3 — A reflash the registry has not heard about is loud.** When the
  id changes between two reads in one daemon lifetime, or mismatches the
  registry, log at WARNING and set a `/status` flag. This is the automatic
  version of what 2026-09-21 needed a human to notice.
- [ ] **W02.4 — Readiness block in `/status`.** One object, `tuning_readiness`,
  each entry `{ok: bool | null, value, detail, fix}`: `ups_soc` (from
  `/control/power`; floor configurable, default 40 %), `storage_free_mb`,
  `memory_channels` (count verified vs withheld, from `memory_withheld`),
  `calibration` (W02.2), `vehicle_state`. `fix` is the sentence the UI shows.
  Status only — nothing reads this to refuse an action except W07.

## Frontend

- [ ] **W02.5 — `useTuningContext`.** Group-scoped state: `selectedLogId`,
  `loadedRom` (the parsed view `RomMapsPanel` currently holds in component
  state). Persist `selectedLogId` with `useLocalStorage`; do **not** persist
  the ROM — `docs/offline-rom-targets.md` says the parsed view is deliberately
  not kept across reloads, and the file never leaves memory on the daemon.
  Lifting it to group state keeps it across *navigation within the group*,
  which is the new behaviour wanted, without changing that guarantee.
- [ ] **W02.6 — `TuningContextBar`.** Vehicle name, running calibration with a
  mismatch badge, active capture with elapsed time, selected log, loaded ROM,
  and the readiness chips. Each chip: green / amber / red / grey-unknown; red
  and amber show `fix` on hover and on tap. Tapping the capture segment goes to
  Logging, the log segment to Logs, the ROM segment to ROM Maps.
- [ ] **W02.7 — Unknown is not OK.** A chip whose value is null renders grey
  with "not reported", never green. *Test:* a `/status` with no
  `tuning_readiness` key renders every chip grey, not absent and not green.

## Empty states

| condition | the bar says |
|---|---|
| no capture active | `capture: none` — links to Logging |
| no log selected | `log: none selected` — links to Logs |
| no ROM loaded | `ROM: none loaded` — links to ROM Maps |
| calibration id unread | `running: unknown (ECM not yet read)` |
| calibration mismatch | `running: 1ZS44A00000 — not in flash history` + link to Calibration |

## Done when

Loading any `tuning-*` route directly shows the bar fully populated or with a
named empty state per segment; selecting a log on Logs and navigating to
Analysis keeps it selected; a mismatched calibration is visible without opening
any page.

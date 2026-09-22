# W02 — the Engine Tuning context bar

Roadmap task W2. Depends on W01. One short KOEO probe on the truck (W02.1);
everything else is desk work.

```text
2008 Xterra · running ZS44A-20260920 · capture: tuning 00:12:41 · log: <selected> · ROM: <loaded>
```

This strip is what lets the Engine Tuning pages be visited in any order: they
share a subject without sharing a sequence.

**Files:** new `panels/tuning/TuningContextBar.tsx` and
`hooks/useTuningContext.ts`; `panels/Workbench.tsx` (render the bar above any
`tuning-*` item); backend `src/dashboard/status.py` (`build_status`),
`src/collection/tuning_profile.py` (`snapshot()` already carries
`running_tune_revision`).

## Measure before designing

- [~] **W02.1 — Does `21 10` answer outside session `0xC0`?** The calibration
  id read (`0x21 10` → ASCII `1ZS44A00000`) was only ever sent inside the
  Mode-22 session. Whether it also answers in the default session decides the
  whole design of W02.2: if it does, the daemon can read it at start-up and on
  every reconnect for free; if not, it has to ride the DID window. One KOEO
  probe with `tools/probe_local_identifier.py`, engine off, no session entered.
  Record the result in `data/dids.yaml` beside the `access` block.
  *Partial (2026-09-21, Pi unreachable):* the answer is now data rather than
  code — `access.calibration_id` in `data/dids.yaml` (`CalibrationIdAccess`
  in `src/decode/dids.py`, `session: default | extended | unverified`, a
  required `evidence` string) records `session: unverified`, and W02.2 reads
  per that field (`unverified`/`extended` ride the Mode-22 window; `default`
  reads at connect). **Remaining:** run the KOEO probe with
  `tools/probe_local_identifier.py` (engine off, no session entered, send
  `21 10`), then flip `session:` to `default` (answers) or `extended` (NRC),
  update `evidence:`, and update the pinned value in
  `tests/test_ecu_calibration_id.py::test_committed_catalog_records_the_session_requirement_as_unverified`.

## Backend

- [x] **W02.2 — Read the calibration id and surface it.** On connect and after
  any reconnect, read `21 10` once (per W02.1's answer) and cache it. Add to
  `/status`: `ecu_calibration_id` (raw ASCII), `ecu_calibration_read_at`, and
  `calibration_matches_registry` — true when the id is consistent with the
  newest entry in `tune_revisions.yaml`, false when not, null when either side
  is unknown. The registry keys revisions by name (`ZS44A-20260920`) and the
  ECU reports a part-number-style id (`1ZS44A00000`), so this needs a
  `calibration_id` field added to `TuneRevision`; do **not** try to derive one
  from the other by string matching. *Test:* null / match / mismatch.
  *Done:* `src/collection/calibration_id.py` + `_read_calibration_id` in the
  Canable loop (read at every loop start = connect/reconnect, retried every
  30 s until answered, refreshed every 5 min); `calibration_matches()` in
  `src/analysis/tune_revisions.py` is exact equality only;
  `calibration_status()` in `src/control/calibration.py` feeds both `/status`
  and `/control/calibration`. Tests in `tests/test_ecu_calibration_id.py`:
  `test_status_calibration_is_null_when_the_ecu_is_unread`,
  `test_status_calibration_is_null_when_the_registry_records_no_id`,
  `test_status_calibration_matches`, `test_status_calibration_mismatch_is_flagged`,
  `test_session_read_rides_a_window_and_is_cached`,
  `test_default_session_read_goes_out_at_connect_before_any_window`,
  `test_an_unanswered_read_is_retried_not_abandoned`.
- [x] **W02.3 — A reflash the registry has not heard about is loud.** When the
  id changes between two reads in one daemon lifetime, or mismatches the
  registry, log at WARNING and set a `/status` flag. This is the automatic
  version of what 2026-09-21 needed a human to notice.
  *Done:* `tuning_profile.record_ecu_calibration_id` logs WARNING on a change
  and (once per pair) on a registry mismatch; `/status` carries
  `ecu_calibration_changed`, `calibration_alert`, `calibration_alert_reason`.
  Tests: `test_mismatch_with_the_registry_logs_a_warning_once`,
  `test_an_id_change_within_one_lifetime_warns_and_sets_the_flag`,
  `test_a_broken_registry_degrades_status_instead_of_failing`.
- [x] **W02.4 — Readiness block in `/status`.** One object, `tuning_readiness`,
  each entry `{ok: bool | null, value, detail, fix}`: `ups_soc` (from
  `/control/power`; floor configurable, default 40 %), `storage_free_mb`,
  `memory_channels` (count whose last known-answer verify passed / failed / never ran, from W06.2),
  `calibration` (W02.2), `vehicle_state`. `fix` is the sentence the UI shows.
  Status only — nothing reads this to refuse an action except W07.
  *Done:* `src/control/tuning_readiness.py`; floor at
  `tuning_readiness.min_ups_soc_percent` (default 40); storage floor is
  `collection.storage_warning_mb`; memory counts come from the last verify
  (`memory_verify.json` beside the operational catalog, written by
  `POST /control/memory-channels/verify`). Tests:
  `test_readiness_block_has_every_entry_with_the_same_shape`,
  `test_ups_floor_is_configurable_and_defaults_to_40`,
  `test_memory_channel_counts_come_from_the_last_verify`,
  `test_calibration_and_vehicle_state_entries`,
  `test_readiness_builder_is_additive_only`.

## Frontend

- [x] **W02.5 — `useTuningContext`.** Group-scoped state: `selectedLogId`,
  `loadedRom` (the parsed view `RomMapsPanel` currently holds in component
  state). Persist `selectedLogId` with `useLocalStorage`; do **not** persist
  the ROM — `docs/offline-rom-targets.md` says the parsed view is deliberately
  not kept across reloads, and the file never leaves memory on the daemon.
  Lifting it to group state keeps it across *navigation within the group*,
  which is the new behaviour wanted, without changing that guarantee.
  *Done:* `hooks/useTuningContext.ts`, owned by `Workbench`; `RomMapsPanel`
  takes `loaded`/`onLoadedChange`. Tests (`TuningContextBar.test.tsx`):
  "persists the selected log and not the ROM", "keeps a loaded ROM across
  navigation within the group". Nothing *sets* `selectedLogId` yet — the Logs
  page is still the W04 placeholder.
- [x] **W02.6 — `TuningContextBar`.** Vehicle name, running calibration with a
  mismatch badge, active capture with elapsed time, selected log, loaded ROM,
  and the readiness chips. Each chip: green / amber / red / grey-unknown; red
  and amber show `fix` on hover and on tap. Tapping the capture segment goes to
  Logging, the log segment to Logs, the ROM segment to ROM Maps.
  *Done:* `panels/tuning/TuningContextBar.tsx`, rendered above every
  `tuning-*` item. Tests: "links each segment to the page that fills it",
  "calls out a mismatch with a link to Calibration", "shows the active capture
  with elapsed time, the selected log and the ROM", "colours reported chips and
  shows the fix on hover and on tap", "shows the bar on every tuning-* route
  loaded directly, and nowhere else".
- [x] **W02.7 — Unknown is not OK.** A chip whose value is null renders grey
  with "not reported", never green. *Test:* a `/status` with no
  `tuning_readiness` key renders every chip grey, not absent and not green.
  *Done:* tests "renders every chip grey when /status has no tuning_readiness
  key", "a null value is grey even when ok claims true"; empty-state strings
  in "names every empty segment with its cause".

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

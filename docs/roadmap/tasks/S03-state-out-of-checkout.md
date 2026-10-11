# S03 — Everything the Pi writes lives under `/data/canary`, not in the checkout

**Why.** `canary-update` fast-forwards the Pi's git checkout and refuses on
tracked local edits. The Pi kept making them, because several files it writes
at runtime are tracked in git or live inside the clone:

| Written on the Pi by | File | Tracked? |
|---|---|---|
| dashboard save / old-daemon normalisation | `data/<slug>/capture_profiles.yaml` | yes (31 laptop commits) |
| `refit_behavior_models` job | `data/<slug>/behavior_models.yaml` (+ `.report.json`) | yes |
| `retrain_*` jobs, model upload | `models/<slug>/*/model.manifest.json` (+ binaries) | manifests yes |
| VIN auto-register, startup calibration-ID probe, dashboard rename | `profiles.yaml` | yes |
| dashboard Custom PIDs | `data/<slug>/custom_pids.yaml` | no (lands in the clone) |
| every control-plane writer | `data/control/...` (~12 files) | ignored, but in the clone |

The 2026-10-04 update was blocked by `capture_profiles.yaml` (the old daemon
had written two empty `kline: []`), and the operator asked for a model-push
method that "doesn't cause git or updater problems".

**Operator decisions (2026-10-04).**
1. **Capture profiles: layered overrides.** The repo's catalog is the base. A
   profile saved or deleted on the Pi is stored under `/data/canary` as an
   override that wins by name; laptop changes keep reaching the Pi for every
   profile not overridden. "Revert to repo" removes an override. The daemon
   logs when an override shadows a profile that has since changed in the repo.
2. **Trained artifacts are Pi-owned.** Models and `behavior_models.yaml` live
   under `/data/canary/models/<slug>`. The repo copy of `behavior_models.yaml`
   only seeds a fresh install. Git stops tracking model manifests. Laptop-trained
   artifacts travel by the upload path or `scp`, never git.

## Checklist

- [x] **S03.1 One state root.** `daemon.state_dir`: when set, every runtime path
  the YAML does not set explicitly resolves under it. A test classifies every
  `Path` field of `DaemonConfig` as *state* or *tracked*, so a new field cannot
  silently default into the checkout.
- [x] **S03.2 Profile registry overlay.** `profiles.yaml` stays the tracked base
  (laptop-authored vehicle parameters). Pi-side writes (display name, VIN
  auto-register, calibration ID / CVN) go to an operational registry under the
  state root, merged over the base by slug.
- [x] **S03.3 Behavior models are Pi-owned.** `behavior_models.yaml` (and the
  refit report beside it) resolve under the models state dir. The daemon seeds
  it from the tracked copy at startup when absent. `behavior_validation.yaml`
  and `behavior_policy.yaml` are laptop-authored facts, so they are read from
  the tracked profile directory, not "beside the artifact".
- [x] **S03.4 Capture profile overrides.** Base = tracked catalog (or the seed
  for a vehicle without one, in memory, never written into the clone);
  overrides + deletions in the state root; save/delete write only the
  overrides; `revert` removes one; a shadowed-and-changed base profile is
  logged. Every reader goes through one loader that takes both paths.
- [x] **S03.5 Models out of git.** Untrack `models/**/model.manifest.json`, ignore
  `models/` wholesale.
- [x] **S03.6 Pi configuration.** `canary.pi.example.yaml` sets
  `daemon.state_dir: /data/canary`; docs and the updater's comments follow.
- [x] **S03.7 Migrate the dev Pi.** Move `data/control/*` and `models/<slug>` out
  of the clone into `/data/canary`, set `state_dir`, deploy, and check
  `git status` is clean afterwards.
- [x] **S03.8 Edit vehicle profiles from the dashboard** (operator request
  2026-10-10: the Profiles page showed each profile but only renamed it; the
  vehicle weight and every other parameter could not be viewed or edited there;
  same day: a "read VIN from the connected vehicle" button with manual entry as
  the fallback). Edits go to the machine overlay, parameters merge field by
  field, a changed fact needs new evidence, revert mirrors capture profiles, and
  an edit reaches the daemon at its next start, never live.

## Evidence

- **S03.1** `DaemonConfig.state_dir` + `DAEMON_STATE_LAYOUT` / `DAEMON_TRACKED_PATHS`
  (`src/config.py`); `profiles.registry_overlay_path` and `llm_assist.cards_path`
  follow it. `tests/test_state_dir.py` fails on an unclassified path field, and
  checks every state path lands under the root, scoped, after `scope_config`.
- **S03.2** `load_registry` / `save_registry` / `update_profile_name` take the
  `ProfilesConfig`; the overlay stores only fields that differ from the tracked
  entry. The startup calibration probe no longer rewrites the registry when
  nothing changed. Tests: a rename leaves `profiles.yaml` byte-identical; a later
  laptop edit to `mass_kg` still reaches the merged profile.
- **S03.3** `governing_profile_file` (`src/behavior/behavior_models.py`): policy
  and validation come from `data/<slug>/` for an artifact outside the checkout
  when that profile directory exists. `seed_machine_owned_files` copies
  `behavior_models.yaml` once at daemon start, never overwriting.
- **S03.4** `CaptureProfileSources` (catalog + overrides) is the only input to
  the store, the loader and both serving-set readers, so no caller can read the
  catalog without the overrides. `CaptureProfileStore.revert`,
  `overridden_names`; `POST /control/capture/profiles/{name}/revert`; the GET
  listing carries `overrides`. Saves and deletes never write the tracked
  catalog, and a missing catalog is seeded in memory, not on disk.
  *Caveat:* a laptop retrain's serving-set answer (which DIDs and PIDs the
  `default` profile polls) cannot see the Pi's override of `default`. Retrains
  on the Pi see both.
- **S03.5** `/models/` ignored (anchored: `models/` would also match
  `src/models`); both manifests untracked. The CI guard against stranded
  feature specs (`tests/test_manifest_producibility.py`, D02.5) now reads
  `tests/fixtures/deployed_manifests/`, a copy of what the Pi runs, refreshed on
  deploy (that directory's README). The updater's `keep_models` restores the two
  manifests across the pull that untracks them.
- **S03.6** `canary.pi.example.yaml` sets `daemon.state_dir: /data/canary` and
  drops the per-field pins (`models_dir: models` would have won over it).
  `install_pi.sh` creates `control/` and `models/`. AGENTS.md and
  `docs/pi-setup.md` updated.
- **S03.7** 2026-10-04 on the dev Pi (truck off, no capture): `canary-update`
  to `09eae49` on the old config first, then with both services stopped,
  `models/nissan-xterra-2008` → `/data/canary/models/`, the five
  `data/control/nissan-xterra-2008/` files → `/data/canary/control/nissan-xterra-2008/`
  (no collisions), the checkout's stray `audit.jsonl`/`control.token` →
  `/data/canary/control/_from-checkout-2026-10-04/`, and the four daemon path
  pins → `state_dir: /data/canary` (backup `/etc/canary/canary.yaml.bak-pre-s03`).
  After the restart the daemon seeded `behavior_models.yaml` from the tracked
  copy, loaded the same IF/LSTM versions from `/data/canary/models`, and
  `git status --ignored` in the checkout shows only build caches.
  `restart_canary.sh` brings back only `canary`, so `canary-mcp` needed a
  separate `systemctl start` after the stop.
- **S03.8** `PATCH /control/profiles/{slug}`, `POST /control/profiles/{slug}/revert`,
  `POST /control/vin/read` (`src/dashboard/routes/control/system.py`), audit
  actions `profile.update` / `profile.revert` / `vin.read`; no MCP tool, because
  the rename has none. *Merge depth:* `_merge_entry` merges `parameters` key by
  key and `save_registry` stores only changed parameters, each evidence unit
  whole (`mass_kg`+`mass_evidence`; `final_drive`, `gearbox_ratios`,
  `tire_loaded_circumference_m`+`driveline_evidence`), so evidence never pairs
  with a number from the other side of the merge.
  `test_a_later_laptop_edit_to_another_parameter_still_flows_after_a_dashboard_edit`,
  `test_evidence_travels_with_its_fact_across_the_merge`. *Evidence refusals:*
  `EvidenceRequiredError` names the evidence field; reusing the old text is
  refused (`test_changing_mass_without_new_evidence_is_refused_by_name`, the
  three driveline cases). Out-of-range values are refused by
  `VehicleParameters`, naming `parameters.<field>`. *When it applies:* at the
  next daemon start. Every component holds the config `scope_config` built at
  startup, and each session manifest records it. Swapping it mid-run would split
  one session across two values. The response and the page carry `applies`,
  `pending_restart` and `in_use`; nothing restarts on its own, and no running
  manifest is rewritten. *Consumers checked:* derived fuel/trip/power and gear
  channels, the dyno (`workbench_jobs` passes `config.vehicle.parameters`), and
  tire wear (`rolling_circumference`) all read the startup copy. Offline tools
  that resolve the profile themselves (MCP `tuning_analysis`/`behavior`,
  scripts) read the registry at once; `pending_restart` is that gap, shown.
  `fuel_plant.yaml` and the engine simulator keep their own displacement
  (3.954 L) and never read `parameters.displacement_l` (4.0). *Found and fixed:*
  a start that read no VIN (key off, parked, bench) used the `vehicle:` block's
  parameters, the code defaults on the Pi (1900 kg, no driveline), and not the
  registry's. A dashboard edit would have had no effect on such a start.
  `_registered_default` now returns the registry entry under that slug, for the
  daemon and for `resolve_profile_for_tooling`
  (`test_a_start_without_a_vin_runs_on_the_registry_parameters`). Offline
  tooling with no `--profile` now gets the registry's Xterra parameters (mass
  2184.4 kg, driveline set) instead of the code defaults. The feature-cache key
  includes `vehicle_parameters` (`src/features/feature_cache.py`), so cached
  feature frames rebuild once. `derived:GEAR_ESTIMATE` now appears in offline
  frames, but it is excluded from model features, and no deployed manifest
  feature reads mass or driveline. *VIN:*
  `src/vehicle/vin.py` checks 17 characters with no I/O/Q, and the check digit at
  position 9: refused on a North American VIN (WMI 1-5), a warning elsewhere. A
  VIN another profile holds is refused (409). The active profile cannot leave
  the VIN its start read (`ActiveVinMismatchError`): otherwise the next start
  would register a new, empty profile. Resolution happens only at start, so
  nothing switches mid-session. The read goes out on the collector's own bus
  (`src/collection/vin_read.py`, served by `_CanObdLoop._maybe_read_vin` in a
  bus-budget slot of its own, outside a Mode 22 window, never while a capture
  is paused), never on a second handle. It is refused by name: `passive_armed`,
  `ecm_held`, `capture_paused`, `key_off`
  (parked/engine_off/no_data/bench), `replay`, `unsupported`; else `no_answer`,
  `refused`, `implausible`, `not_polling`. It is returned, never saved. 60 tests
  in `tests/test_profile_edit.py`. They include: the loop reading a multi-frame
  VIN on a scripted ECU; a passive loop and a paused capture never taking the
  ticket; the VIN request landing at least one budget slot after the previous
  request; and a ticket submitted inside a session window being served only
  after the exit, in the default session. With the guards removed, the last
  three tests fail. *Verifier fixes (2026-10-10):* the budget slot, the paused
  capture, evidence compared after collapsing whitespace and folding case (a
  trailing space or a capital is not new evidence), and the resolution record
  reset in `reset_process_state_for_tests`. Frontend:
  `ProfilesPanel` shows every parameter with unit, evidence text, tracked vs
  this-machine and restart-pending, per-field and all revert, and the resolution
  reason. `ProfileEditor` has the form, evidence prompts that block Save, the
  VIN read button, a live check-digit status (`util/vin.ts`, which mirrors the
  backend) and the refusal text. DynoView's weight is now the in-use mass, with
  a note when Profiles holds a different one; the "(Profiles)" pointer is now
  accurate. Gate: ruff, format and mypy clean; pytest 391 + 278 passed over the
  profile, state_dir, layering, process-state, route-snapshot, control/dashboard
  API and OBD-loop files;
  vitest 23 passed; pnpm typecheck, lint and format clean.

## Found along the way

**The deployed LSTM has four inputs the serving profile never collects.**
Moving the manifest guard from git's copy (LSTM 2026-09-20) to what the Pi
actually runs (LSTM `2026-09-24T035125`, retrained in place) showed
`obd:mem:ECM_ENGINE_SPEED_RAM`, `obd:mem:ECM_IGNITION_TIMING_RAM` and their
`__mean` columns unproducible: only the `tuning` profile polls RAM channels
(the Xterra `default` has `memory: []`). *Corrected the same day:* they were
near-absent in training as well (4 rows across the nine training sessions), so
the model learned them as missing and the serve-time gap costs little. They
exist only because the coverage floor that drops near-empty channels (D03.34)
landed two days after the model. Recorded as reviewed debt in
`tests/fixtures/manifest_unproducible_allowlist.yaml`, pinned to that model
version; the next retrain drops them, and the entry then fails as stale.

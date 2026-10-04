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

## Found along the way

**The deployed LSTM has four inputs the serving profile never collects.**
Moving the manifest guard from git's copy (LSTM 2026-09-20) to what the Pi
actually runs (LSTM `2026-09-24T035125`, retrained in place) showed
`obd:mem:ECM_ENGINE_SPEED_RAM`, `obd:mem:ECM_IGNITION_TIMING_RAM` and their
`__mean` columns unproducible: only the `tuning` profile polls RAM channels
(the Xterra `default` has `memory: []`). Those inputs are missing while it
scores. The model predates the R03.12 gate that keeps tuning captures out of
training. Recorded as reviewed debt in
`tests/fixtures/manifest_unproducible_allowlist.yaml`, pinned to that model
version; **retrain owed** on default-profile sessions, after which the entry
fails as stale and is deleted.

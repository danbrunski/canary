# R30 — Scripts, tools, test infrastructure and the gates

Part of the 2026-11-12 review ([R22](R22-refactor-README.md)). Scope:
`scripts/`, `tools/`, test scaffolding under `tests/`, the shell gates and
installers, `rust/`, `pyproject.toml`. Structure and duplication, plus one
defect (R30.1). Counts are review snapshots; locate code by symbol. Nothing
here has been implemented. Shared acceptance rules are in R22.

**State of the area.** R06's results hold: `src/` imports nothing from
`scripts/` or `tools/`, scripts do not import each other, and tests do not
edit `sys.path`. The tests themselves are not copy-paste: 8,453 tests average
15.5 lines, nine are 100 lines or longer, and tests identical apart from
literals total 560 lines. The repo root is clean (every stray directory is
ignored and untracked). What regrew is scaffolding: R07.5 and R07.2 each
closed on a one-off grep with no guard, and both counts are back above where
they started.

- [x] **R30.1 `scripts/deploy_truck.sh` restarts the daemon with a bare
  `systemctl restart canary.service`.** `AGENTS.md`: "Use
  `scripts/restart_canary.sh`, never a bare `systemctl restart canary`",
  after a deploy ended another session's capture on 2026-09-21.
  `update_canary.sh` goes through `restart_canary.sh`; this script, which
  `docs/truck-test.md` advertises as the one-command deploy, does not. Its
  other steps are now done by `install_pi.sh`.
  - [x] Call `restart_canary.sh` for the last step, or retire the script into
    `install_pi.sh && restart_canary.sh` and update `docs/truck-test.md`.
    Done: `deploy_truck.sh` is now `install_pi.sh` then `bash
    restart_canary.sh "$@"` (exit code passed through, `--force` forwarded,
    message that the daemon kept running old code on a refusal). Dropped as
    redundant: the `${DATA_DIR}/control` install (`install_pi.sh` creates it
    and `chown -R`s `/data/canary`), the can0 report (`install_pi.sh` runs
    `systemctl enable can0.service` under `set -e`) and `systemctl enable
    canary.service` (`install_pi.sh`). `docs/truck-test.md` notes the refusal.
    `bash -n` clean; script not run (sudo/systemctl).
  - [x] A test that no tracked `.sh` other than `restart_canary.sh` contains
    `systemctl restart canary` followed by `.service` or end of word
    (`canary-mcp` and `canary-provision` are separate services).
    `tests/test_daemon_restart_guard.py` (4 tests, `git ls-files '*.sh'`):
    also catches `restart_if_changed canary` (the installer's `systemctl
    restart "$@"` helper; it is only ever called with gpsd, chrony, journald
    and avahi). Verified it fails on `git show HEAD:scripts/deploy_truck.sh`
    (names the 2026-09-21 incident and `restart_canary.sh`) and passes now.

- [x] **R30.2 Tests import each other's private builders again.** R07.5
  closed with zero `from tests.test_*` imports. There are now 112 in 58
  files: `test_sim_montecarlo` (`build_kit_and_image`, `_design`, `_run`,
  `_template`), `test_target_phi` (`PROFILE`, `IMAGE_SHA`, `_definition`,
  `_session`, `_table`, `_evaluate`, `_viewer`, imported by 12 files),
  `test_brain_jobs` (`scheduler`, `scoped_config`, `write_session`),
  `test_paired_scenarios`, `test_rom_editor`, `test_evidence_memory`, and
  `test_layering` for `LAYERS`. Each import re-executes the provider module,
  and a rename in `test_target_phi` breaks twelve files.
  - [x] First the guard R07.5 lacked: an AST test that fails on
    `from tests.test_…`, with today's 58 files in an allowlist that may only
    shrink. `tests/test_source_rules.py` row `tests-import-other-tests`
    (AST: `from tests.test_x`, `from tests import test_x`, `import
    tests.test_x`, function-level included): 58 files / 118 import
    statements at HEAD, per-file counts, ratchet both ways.
  - [x] `tests/sim_builders.py`, `tests/target_phi_builders.py`,
    `tests/evidence_builders.py`; `scheduler`, `write_session` and
    `submit_twin` into `tests/builders.py`, under public names. `LAYERS` gets
    an importable home outside a test module.
    **Done 2026-11-15.** Every provider's shared names moved with their bodies
    (and whatever module-level code they need) out of 45 test modules into 15
    plain modules under `tests/`: `sim_builders` (montecarlo, paired scenarios,
    editor requests, cells, design, effects, timing tables, rom_sim_cli),
    `target_phi_builders`, `twin_builders`, `twin_live_builders`,
    `evidence_builders`, `hypothesis_builders`, `candidate_builders`,
    `provisioning_builders`, `kline_builders`, `imu_builders`, `dyno_builders`,
    `capture_builders`, `sequence_builders`, `rom_editor_builders`, `layers`
    (`LAYERS`), plus `scheduler`/`write_session`/`submit_twin`/`SESSION`/`SLUG`,
    the brain routes' `AUTH`/`TOKEN`/`brain_mcp_client`, `state_dir_config` and
    the trip-digest executor into `tests/builders.py`, and `FakeAdc` into
    `tests/fakes.py`. Private names went public (`_session` -> `phi_session`,
    `_design` -> `mc_design` / `cells_design`: where two providers shared a name
    in one module the provider's topic prefixes it); fixtures kept their names
    and are re-imported `as` themselves so pytest still finds them. Eight
    importers used the module form (`from tests import test_dyno as td`,
    `ordering._config`, `w09.request`, `kit_and_image = test_sim_montecarlo.
    kit_and_image`): converted to named imports first. Three constants that
    recomputed the repo root from `Path(__file__)` now use `REPO_ROOT`
    (`tests-recompute-repo-root` saw them again after the move).
    **Guard:** the `tests-import-other-tests` allowlist is gone (58 files -> 0)
    and the rule also reads a script a test hands to a subprocess (six such
    strings imported a provider: repointed, a test's own module is the one
    thing allowed). **Two things the move had to respect.** The `imports
    nothing that talks to the ECU` tests import a test module in a subprocess
    and inspect `sys.modules`: `rom_editor_builders` (dashboard routes) and
    `twin_live_builders` (`src.daemon.twin_live`) are separate modules so
    `sim_builders` / `twin_builders` stay light (9 of those failed when they
    shared a module). And a rename must skip keyword arguments and method
    names, not only attributes: the first pass renamed a `calibration=` keyword
    and a `Drives._session` method; `mypy` caught both.
    Evidence: `pytest --collect-only -q tests`: 13712 ids before, 13727 after,
    every old id present, the 15 added are `test_no_test_edits_sys_path[<new
    module>.py]`; touched test files (86) plus `test_source_rules`,
    `test_layering`, `test_script_imports`: 3498 passed, 1 xfailed (before the
    two subprocess-split fixes, 9 failed and 119 errored on fixtures not
    re-imported); `ruff check .`, `ruff format --check .`, bare `mypy`
    ("no issues found in 1444 source files"). `grep` for `from tests.test_`
    outside strings finds nothing.
- [x] **R30.3 "Scope the config to the tooling profile" is spelled at about
  25 call sites.** `scripts/_cli.py` has `scoped_config` and
  `load_scoped_config`. `scripts/catalog.py`, `fingerprint.py` and
  `trip_report.py` carry identical `_scoped_config` copies;
  `tuning_report._scoped` is a drifted one without the `profiles.enabled`
  guard. Twelve scripts, nine tools and four `src/` modules call
  `scope_config(` directly, and `scripts/sync-train.sh` does it in a Python
  heredoc. Each copy is a place the P02.11 rule (refuse an unscoped config
  while profiles are enabled) can be forgotten. `tools/` never got the
  scripts' preamble because `_cli` imports pandas: eight tools repeat the
  same `--config` / `--profile` pair and eleven do
  `sys.path.insert(0, str(REPO_ROOT))` inside `main()`.
  - [x] `scoped_config_for_tooling(config, slug)` in `src/vehicle/profiles.py`
    (or wherever R26.4 puts path scoping); `_cli.scoped_config` re-exports it;
    delete the four `_scoped*` functions; migrate the direct callers.
    **Done 2026-11-15.** `scoped_config_for_tooling` is in
    `src/vehicle/profiles/resolve.py`, beside `resolve_profile_for_tooling`
    and re-exported by the package. Not `scope.py`, R26.4's home for path
    scoping: it calls `resolve_profile_for_tooling`, and `resolve` imports
    `scope`. It keeps the P02.11 rule: profiles enabled returns a config that
    always carries `scoped_profile` (so `require_profile_scoped` passes; the
    unscoped config it replaces raises `UnscopedConfigError`), profiles
    disabled returns the config untouched, scoping twice is a no-op, and
    re-scoping to another slug raises. `scripts/_cli.scoped_config` *is* the
    function (`scoped_config = scoped_config_for_tooling`);
    `scoped_models_dir` and `apply_since_boundary` go through it too. Deleted:
    `_scoped_config` in `catalog`, `fingerprint`, `trip_report` and
    `tuning_report._scoped` (now `load_scoped_config(args, ...)`).
    **`tuning_report._scoped` had no `profiles.enabled` guard** (it scoped a
    profiles-disabled config under the default slug, a path the daemon never
    wrote); the migration adds it, so a normal config is unaffected and a
    profiles-disabled one keeps its base paths. Same for the other unguarded direct callers, all moved onto
    the guarded function: scripts `analyze_session`, `evaluate_behavior_models`,
    `inventory_behavior_inputs`, `backfill_rolling_circumference`; tools
    `mint_comparison`, `record_command_change`, `memory`,
    `investigate_delivery`, `change_detection_report`, `import_uprev_log`.
    Already-guarded callers moved: scripts `refit_behavior_models`,
    `vehicle_log`, `disposition_report`, `scripts/sync-train.sh`'s heredoc;
    tools `check_manifest_producibility`, `inventory_behavior_artifacts`,
    `migrate_channel_names`; `src/plausibility/benchmark.py` and
    `src/features/feature_policy.py` (training/evaluation tooling, not daemon
    paths). Left on `scope_config` on purpose: `src/main.py` (the daemon,
    scoping with the profile the VIN probe resolved),
    ~~`src/mcp_server/tools/behavior.py` and `tuning_analysis.py`~~ (moved
    onto the function 2026-11-16, below),
    `src/validation/harness.py` (`harness_config` builds the config
    `run_daemon` runs on, scoped "like the daemon") and
    `scripts/migrate_to_profiles.py` (plans the move to one named profile, the
    primitive itself). Consequence for profiles-disabled configs only: the
    memory-ledger tools (`memory`, `mint_comparison`, `investigate_delivery`,
    `change_detection_report --config`) now refuse with `UnscopedMemoryError`
    where they used to scope under the default slug; `memory_ledger_for`
    refuses an unscoped config. `record_command_change` refuses too, with a
    bare `AssertionError` ("scope the config to a vehicle profile",
    `record()`'s `assert config.scoped_profile is not None`).
    **Operator, 2026-11-15: keep the refusals** (profiles default on and no
    shipped config disables them; the old "scope under the default slug"
    read a directory nothing writes and printed "0 record(s)"), **and make
    them clean.** Done: `tools._cli.load_scoped(args, per_vehicle=True)`
    exits with one line (`refused: <config> has profiles.enabled: false, and
    this tool reads the per-vehicle memory ledger ...`, exit 1, no traceback);
    all five tools pass it. Pinned by `tests/test_tooling_config_scope.py`:
    `::test_a_per_vehicle_tool_refuses_a_profiles_disabled_config_in_one_line`,
    `::test_a_per_vehicle_tool_runs_on_a_profiles_enabled_config`,
    `::test_the_memory_tool_refuses_without_a_traceback` (runs the tool as a
    file), and `::test_every_memory_ledger_tool_loads_its_config_per_vehicle`,
    which finds every `tools/*.py` that calls `memory_ledger_for` and fails if
    any `load_scoped` call there omits `per_vehicle=True` (fails on
    `mint_comparison` with the flag removed). `record()`'s assert stays as an
    internal invariant; the CLI refuses before reaching it. Also found: on a profiles-disabled config
    `scripts/analyze_session.py` read base paths while its MCP twin
    (`mcp_server/tools/behavior.py`, and `tuning_analysis._scoped`) nested
    under the default slug; fixed by the last sub-item below (2026-11-16).
    Guard:
    `tests/test_source_rules.py` row `tooling-scopes-its-own-config` (no
    `scope_config(` in `scripts/` or `tools/`, `.py` by AST and `.sh` by line;
    one allowlisted: `migrate_to_profiles.py`). Pinned by
    `tests/test_tooling_config_scope.py::test_a_profiles_enabled_config_comes_back_scoped`,
    `::test_a_slug_picks_the_profile_and_scoping_twice_is_a_no_op`,
    `::test_a_profiles_disabled_config_is_left_alone`,
    `::test_the_scripts_scope_through_the_same_function`;
    `test_tire_wear.py` now stubs `scoped_config_for_tooling` in the backfill.
  - [x] Move the two MCP `_scoped` paths (`mcp_server/tools/behavior.py`,
    `tuning_analysis.py`) onto `scoped_config_for_tooling` so the MCP tools and
    their script twins scope alike. Found verifying R30.3, 2026-11-15.
    **Done 2026-11-16.** Same semantics as the twins, checked before replacing:
    both MCP tools take a config *file* path (`CanaryConfig.from_yaml(config_path)`)
    exactly as `scripts/analyze_session.py` / `tuning_report` load theirs, and
    both resolved the profile through `resolve_profile_for_tooling(config, None |
    profile)`, so they agree with `scoped_config_for_tooling` on a profiles-enabled
    config. The one difference was the `profiles.enabled` guard: on a
    profiles-disabled config `scope_config` nested the session root under the
    default slug (`<storage>/nissan-xterra-2008`, measured) where the script reads
    `<storage>`. No different input is needed: the daemon's live config is not
    involved, these are the local read tools. `behavior.get_behavior_deviations`
    scopes with `scoped_config_for_tooling(CanaryConfig.from_yaml(path), None)`;
    `tuning_analysis._scoped(path, profile)` (used by `get_delivery_error` and
    `compare_tuning_sessions`) is now a one-line wrapper over it. Behavior change:
    a profiles-disabled config now reads its base session root from the MCP
    tools too (it read a directory nothing writes). Guard: the
    `tooling-scopes-its-own-config` row of `tests/test_source_rules.py` now also
    covers `src/mcp_server/tools`. Pinned by
    `tests/test_tooling_config_scope.py::test_the_mcp_read_tools_scope_as_their_script_twins_do[enabled|disabled]`
    (stubs each tool's `resolve_session`, compares the session root it is handed
    with `scripts._cli.scoped_config`; the `disabled` case fails on `scope_config`)
    and `::test_the_mcp_tuning_tools_take_a_profile_slug_as_the_scripts_do`.
    That was the last open R30.3 sub-item: R30.3 is ticked below.
  - [x] A pandas-free `tools/_cli.py` with `add_config_profile_args` and
    `load_scoped`.
    **Done 2026-11-15.** `tools/_cli.py` imports `src.config` and
    `src.vehicle.profiles` only, and puts the repo root on `sys.path` as
    `scripts/_cli.py` does; a tool imports it with the scripts' `try: from
    _cli import ... except ImportError: from tools._cli import ...` (the first
    form is how `.venv/bin/python tools/x.py`, the way every doc runs them,
    finds it). Seven tools build `--config` / `--profile` through
    `add_config_profile_args` (`check_manifest_producibility`,
    `inventory_behavior_artifacts`, `mint_comparison`, `record_command_change`,
    `memory`, `investigate_delivery`, `migrate_channel_names`, whose config is
    optional and whose help is its own); `change_detection_report` kept its
    own pair until R30.4 moved it onto `add_profile_dir_args`. The `sys.path.insert(0, str(REPO_ROOT))`
    in `main()` is gone from nine tools (the seven above plus
    `make_investigation_cases` and `audit_artifact_lineage`, which import
    `src` only, installed editable) and from `change_detection_report`'s module
    level; `replay_behavior_compare` keeps its module-level shim, which it
    needs for `from tools.replay_event_diff import ...` run as a file. Run as
    a file from another directory, `--help` of every tool above works.
    Pinned by `tests/test_tooling_config_scope.py::test_tools_cli_imports_no_pandas`
    (subprocess: `import tools._cli`, `'pandas' not in sys.modules`),
    `::test_the_config_and_profile_pair_has_one_shape`,
    `::test_load_scoped_reads_the_flags_and_scopes_with_profiles_enabled`,
    `::test_load_scoped_leaves_a_profiles_disabled_config_unscoped`,
    `::test_a_tool_run_as_a_file_finds_its_cli_preamble` (12 tools, subprocess),
    `::test_a_tool_never_inserts_the_repo_root_inside_a_function` and
    `::test_a_bare_cli_import_in_a_tool_falls_back_to_the_package`.
  - [x] `tools/replay_window.py` never scopes its config. Found verifying
    R30.6, 2026-11-15. On a normal config (profiles enabled) it dies with
    `UnscopedConfigError: featurize_wide_frame needs a profile-scoped
    config`, and it runs only with `profiles.enabled: false`. Migrate it with
    the other tools, and add a test that runs it on a profiles-enabled config.
    **Done 2026-11-15.** `main` loads through `load_scoped(args)` (flags
    unchanged: no `--profile`, so the `vehicle:` block's profile), inside the
    existing `except (FileNotFoundError, KeyError, ValueError)` that prints
    `error loading config`. The session, the default IF and LSTM artifact
    paths and the anomaly log it reads are now the vehicle's, under
    `<base>/<slug>/`. `tests/test_tooling_config_scope.py::
    test_replay_window_scopes_its_config_when_profiles_are_enabled` runs
    `tools.replay_window.main` on a profiles-enabled config with the session
    under `<storage>/<slug>/`, the library stubbed to call
    `require_profile_scoped(config, reader="featurize_wide_frame")` first;
    it fails on HEAD's tool (`error: session not found`, exit 1) and passes
    now. `::test_replay_window_still_runs_on_a_profiles_disabled_config` keeps
    the other shape. The `JobController` still passes no `--profile`
    (`src/control/jobs.py::_replay_window_command`), so a second vehicle's
    replay scopes to the config's default profile.
  - Pinned by `test_script_cli_snapshot.py`, `test_script_imports.py`,
    `test_bus_tool.py` (all three pass unchanged), and
    `tests/test_tooling_config_scope.py`.

- [x] **R30.4 `--profile` means a slug in some tools and a directory in
  others, and ten tools default to the Xterra and one operator's home.**
  Slug: `_cli.add_profile_arg`, `tools/memory`, `mint_comparison`,
  `investigate_delivery` and five more. Directory path:
  `tools/fuel_plant_check`, `manifold_check`, `plant_validation`,
  `fuel_comp_samples`, `fuel_comp_restore_reproduction`, `fuel_comp_fit`,
  `rom_sim`. Those, with `timing_lookup_coverage` and `bench_ecu_twin`,
  default to `REPO/"data"/"nissan-xterra-2008"` and
  `Path.home()/"canary-data"/"sessions"/"nissan-xterra-2008"`. `AGENTS.md`:
  "Don't bake Xterra-isms into shared code paths". A second Pi on a 2013
  Xterra exists.
  - [x] One `add_profile_dir_args` that takes a slug, resolves `data/<slug>`
    through `vehicle_data_file` (so platform files are found), and takes the
    sessions root from the config or `--sessions`. No default slug.
    **Done 2026-11-16.** `tools/_cli.py` (still pandas-free) has
    `add_profile_dir_args(parser, sessions_flag="--sessions-root" | None)`,
    `load_profile_data(args) -> ProfileData` (`slug`, `directory`,
    `sessions_root`, `config`, `.file(name)`, `.require_sessions_root()`) and
    `profile_data_or_exit(args, need_sessions=)`. Flags: `--profile SLUG`,
    `--config`, the sessions flag, `--data-dir` (where `<slug>/` directories
    live; default the checkout's `data/`, there for a synthetic profile in a
    test). `--profile` wins, else the scoped `--config`'s profile, else the
    refusal `refused: --profile SLUG is required (the directory name under
    data/); there is no default vehicle ...`, one line, exit 1. A value with a
    separator is refused by name (`--profile takes a slug, not a path (got
    'data/nissan-xterra-2008'); use ... --profile nissan-xterra-2008`), so a
    stale invocation says what to write; an unknown slug is `no profile
    directory <path> for --profile <slug>`. Sessions root: the sessions flag,
    else the scoped `--config`'s `collection.storage_path`, else
    `require_sessions_root()` refuses naming `--sessions-root` and `--config`.
    `ProfileData.file(name)` is `vehicle_data_file` through the config's make,
    model and year (a profile's own file wins; a platform file is refused
    without `--config`, naming that). The tools' library loaders
    (`load_fuel_plant`, `load_kit`, ...) still take the directory, so none of
    them yet reads a platform file; `.file` is there for the first that
    needs one. Converted (flags are `--profile SLUG --config --sessions-root
    --data-dir`): `fuel_plant_check`, `manifold_check` (flags before the
    subcommand; `--sessions-root` stays on `breathing-bound`), `plant_validation`,
    `fuel_comp_samples`, `fuel_comp_restore_reproduction` (sessions may be paths,
    so the root is needed only for a bare name), `fuel_comp_fit`,
    `timing_lookup_coverage` (its `--profile-dir` is now `--profile`),
    `change_detection_report` (own `--profile`/`--config`/`--sessions-dir` pair
    replaced; `--sessions-dir` is the sessions flag), `rom_sim` (a slug, not a
    directory: no documented procedure used the directory form; `design drives`
    takes `--sessions-root`/`--config` for a bare session name). Made
    required: `audit_artifact_lineage --profile`, `scripts/timing_compare.py
    --sessions-root` (a refusal when `--*-sessions` are given without it).
    `bench_ecu_twin` has no vehicle flag: its profile belongs to the committed
    ground-truth capture it replays, so `tests/fixtures/ground_truth/target_phi.yaml`
    now states `vehicle: nissan-xterra-2008` and the tool reads it there.
    Pinned by `tests/test_tooling_config_scope.py`:
    `::test_the_profile_flags_have_no_default_vehicle`,
    `::test_a_missing_profile_refuses_in_one_line`,
    `::test_a_profile_path_is_refused_by_name_not_looked_up`,
    `::test_a_slug_resolves_under_the_data_dir_and_an_unknown_one_refuses`,
    `::test_the_config_gives_the_profile_and_the_sessions_root`,
    `::test_a_profiles_disabled_config_cannot_name_the_profile`,
    `::test_a_platform_file_is_found_through_the_configs_vehicle`,
    `::test_a_vehicle_data_tool_refuses_without_a_profile_and_takes_no_path[x8]`
    (every converted tool but `rom_sim`), `::test_rom_sim_refuses_a_missing_profile_by_name`,
    `::test_a_drive_design_needs_a_sessions_root_only_for_a_bare_session_name`;
    the seven new tools join `SCOPING_TOOLS` (run as files, no `sys.path` edit
    in a function). Tests of the converted tools pass `--profile` explicitly:
    `tests/sim_builders.profile_flags` (a synthetic profile is its directory
    name plus `--data-dir <parent>`), `REAL_PROFILE` in `test_rom_sim_cli.py`
    (the committed Xterra slice), `test_change_evaluation.py`,
    `test_timing_lookup.py`, `test_rom_sim_private_rom.py`, `test_twin_runner.py` (the slow `fuel_plant_check identify` check, passed). Not converted, on
    purpose, with the reason in each place: `scripts/canary_desk.py`
    (`DEFAULT_PROFILE`, `DEFAULT_STATE_DIR=~/canary-data/desk`: the desk app's
    own default vehicle and state, a product decision) and
    `scripts/e06_pi_bench.sh` (`E06_SESSION` names the October-4 capture the
    E06 trial replays, on the Pi).
  - [x] Regenerate the CLI snapshot on purpose.
    **Done 2026-11-16.** `CANARY_UPDATE_CLI_SNAPSHOT=1 .venv/bin/python -m
    pytest -n0 tests/test_script_cli_snapshot.py`; the snapshot covers
    `scripts/` only, so the diff is one line: `timing_compare.py
    --sessions-root` default `"/data/canary/sessions/nissan-xterra-2008"` ->
    `null`. (The `tools/` CLIs are not snapshotted.)
  - [x] A guard: `tests/test_source_rules.py` row `tooling-defaults-no-vehicle`
    refuses, in `scripts/` and `tools/` `.py`, a default naming a vehicle
    slug (a `data/*/` with an `afr_calibration.yaml`), a platform id
    (`data/platforms/*/`) or `canary-data`, in these forms: argparse `default=`
    and `set_defaults(...)`, a function parameter or keyword-only default, a
    class-body (dataclass) field, a plain or annotated `DEFAULT_*` / `PROFILE*` /
    `SESSION*` assignment, and any module constant holding such a value when it
    is used as one of those. Pinned by `::test_the_vehicle_default_rule_catches_every_form`
    (11 forms, each with a clean twin) beside the row's own bad/good pair.
    Known blind spots, in `_vehicle_default`'s docstring and the rule's `why`: string
    concatenation or an f-string over the slug's parts, an `os.environ` fallback,
    and `.sh` files (`e06_pi_bench.sh`). One allowlisted file:
    `scripts/canary_desk.py` (4 hits: the two constants and the two argparse
    defaults spelled through them).
  - [x] Verifier follow-up, 2026-11-17. `tools/audit_artifact_lineage.py` took
    `--profile nope` and `--profile data/nissan-xterra-2008` and exited 0 with a
    report (`discover()` builds `<repo>/data/<slug>` unchecked); it now runs the
    slug through `load_profile_data` (data dir `<--repo>/data`) and refuses in one
    `refused:` line, exit 1, and joins
    `::test_a_vehicle_data_tool_refuses_without_a_profile_and_takes_no_path`
    (`--profile` is no longer argparse-required, so a missing one is the same
    one-line refusal). Two more live Xterra defaults:
    - `tools/rom_extent.py` `DEFAULT_RECIPE = PLATFORMS_DIR/"nissan-xterra-n50"/...`
      is gone. `--recipe PATH` still names a file; otherwise `--profile` / `--config`
      (`add_profile_dir_args`, no sessions flag) find `rom_backup.yaml` through
      `ProfileData.file` (the profile's own, else its platform's by the config's make,
      model and year). `load_profile_data` / `profile_data_or_exit` gained
      `require_directory=False` for it: a vehicle of a covered generation need not
      have a `data/<slug>/`, but only for the vehicle the config itself describes
      (`resolve_profile_for_tooling(config, None).slug`): any other slug with no
      directory refuses (`no profile directory <path> and --profile <slug> is not the
      config's vehicle (<own>)`), so a typo cannot fall through to the platform's
      recipe and hide a vehicle's own override. Without `--recipe` and `--config` it
      refuses naming `--config`. Verified with `canary.dev.example.yaml --profile nissan-xterra-2008`.
      Pinned by `tests/test_rom_extent.py::test_the_cli_finds_the_recipe_from_the_profile_through_its_platform`;
      the three existing CLI calls pass `--recipe`.
    - `tools/emit_dbc.py` `--out` is required (was `data/dbc/xterra_2008.dbc`). Not
      derived from a profile: its input `data/bindings.yaml` is repo-wide, not per
      vehicle, so no profile or config names the output. The shipped file is named in
      the help and the usage lines; the documented regeneration is now
      `tools/emit_dbc.py --out data/dbc/xterra_2008.dbc` (docstring updated; no other
      doc or `.claude` file invokes it).
    The widened rule did not flag `emit_dbc` (`xterra_2008` is not a slug string);
    `rom_extent` is covered by the new platform-id form.
  - [x] Operator decision: whether `scripts/canary_desk.py` should lose its
    Xterra default profile and `~/canary-data/desk` state dir, and whether
    `scripts/e06_pi_bench.sh` should take its session through a required env var.
    **Decided 2026-11-16: keep both.** The desk is an app with its own state,
    and requiring the flags would break a fresh desk start (its allowlist row
    in `tooling-defaults-no-vehicle` stays); `E06_SESSION` names the fixed
    capture the E06 trial replays.

- [x] **R30.5 `tools/rom_sim.py` runs a second copy of the ROM-edit
  simulation.** 1,080 lines: `_World`, `_template`, `_compare`, `_trace`,
  `cmd_run` (153 lines), `edit_rows`, and `SLOT_SOURCE`, `LEARNED_SOURCE`,
  `LEARNED_HELD`. `src/analysis/rom_editor.py` has `_simulate`, its own
  `_template`, the same constants, and `simulate_edits`, which the dashboard
  and the MCP tool use. The two `_template` functions build the same
  `PairedScenario` and differ only in where the learned values come from.
  A new `LearnedState` field has to be added twice, and the 1,281-line
  `test_rom_sim_cli.py` pins the tool's copy while the dashboard exercises
  the other. This is the same finding as R25's "two simulate drivers", seen
  from the tool; do them as one change.
  - [x] Lift the template builder, the kit / base / candidate loader and
    compare / trace into `src/analysis/`. The tool keeps argparse, thin
    `cmd_*` adapters and its "never write inside the checkout" policy.
    **Done 2026-11-15.** R25.3 had already shared `scenario_template` and
    `simulate_cells`; what was still doubled moved like this:
    - **`rom_editor`** holds the shared pieces. `compare_images(…, in_mode=)`
      replaces the editor's `_effects` and the tool's `_compare`.
      `spec_candidate` / `file_candidate` give one trace identity (model,
      slot, `SLOT_SOURCE`) for the editor's `_candidate` and both CLI routes.
      `learned_state(values, origin=)` is the one `LearnedState` builder.
      `LEARNED_SOURCES` holds both drivers' source strings.
      `LEARNED_HELD` (moved from the tool) feeds `cli_learned`, and
      `rom_editor._check_learned_held()` runs at module level and refuses the
      import (TypeError) if a `LearnedValues` field is added without a
      `LEARNED_HELD` key or the reverse; verification found the first draft
      only failed at the first `cli_learned` call, so the check was added
      (`tests/test_rom_sim_world.py::test_a_learned_field_the_cli_does_not_hold_is_refused_at_import`).
      The template is now parameterized only by `origin`.
    - **New `src/analysis/rom_sim_world.py`.** `SimWorld.load` brings up the
      declared kit, the uncertainty, the slot and the resolver
      (`no_tune_registry`). `load_base`, `candidate`, `compare`, `trace` and
      `template` sit on it, beside `load_spec` (`edit_spec_not_found`).
    - **The tool** keeps argparse, the flag-naming refusals (`_base_ref`,
      `candidate_unstated`, `_need`, `_run_bounds`, `_finite`), `_out_dir`,
      `_publish`, `edit_rows` and its own result document. Its
      `LEARNED_HELD` / `LEARNED_SOURCE` / `SLOT_SOURCE` copies are gone, and
      result.json records `template.learned.source`. 1,060 → 952 lines.
    - **Refusal order is unchanged,** the same as `_World`: profile, kit,
      registry, which candidate, spec, base, candidate.
    - **New `tests/test_rom_sim_world.py`, 5 tests.** An AST check that the
      tool calls none of `LearnedState` / `compare_roms` / `build_candidate` /
      `load_sim_kit` / … and assigns none of the three constants, plus its
      non-vacuity twin. The two drivers' learned states differ only by
      `source`. `SimWorld` traces and templates on the declared slot and
      refuses by name.
    - **`tests/test_rom_editor.py` `_PLUMBING`:** the rom_editor string count
      goes 1 → 2, because `LEARNED_HELD`'s keys now live in `src/`. It is a
      stated 0.0, not a read.
    - **The CLI's output is identical to HEAD dcdd1bc3,** run from a scratch
      worktree. Stdout was compared byte for byte with `wall_seconds`
      dropped; it was already `sort_keys` JSON. Output files were compared by
      sha256.
      - *Every `rom_sim.main` call the tests make:* a logging plugin over
        `test_rom_sim_cli`, `test_ignition_timing`, `test_rom_levers_parity`,
        `test_rom_editor_api`, `test_sim_assumptions`, `test_sim_montecarlo`,
        `test_paired_refusals`, `test_rom_editor`, `test_rom_sim_private_rom`,
        `test_rom_sim_known_answers` and `test_rom_editor_private_rom`, with
        the same `--basetemp`. That is 139 calls: 31 `design grid`, 13
        `design drives`, 46 `inspect` and 49 `run`; 58 exit 0, 81 exit 2. All
        139 have the same exit code and stdout, and all 78 hashed output
        files match. 8 calls differ only in argv, by the tree's own fixture
        path.
      - *30 real invocations:* the Xterra profile, the operator's
        ZS44A-20260920/20261004/20261110 images and the
        `_capture-tuning-2026-09-23T142926` drive. They cover `design grid` ×2
        and `design drives`, 5 `inspect`s (edit spec ± timing/regime, K ×1.05,
        two `--candidate-rom`), 6 `run`s (seeds 1/3/5/11/42, fuel_comp and
        target_afr grids, ecu_reflash, HI DET set/fallback and clear/active,
        a drive design) and 16 refusals. 29/30 stdouts match, and so do all
        24 files (designs, result.json, traces.parquet, report.html). The
        30th is `inside_checkout`, whose message names each tree's own
        checkout path.
    - **ROM Editor answers are identical to HEAD.** On the 20261004 image, 6
      answers match after `sort_keys`: `preview_edits` (Fuel Comp add_raw;
      K ×1.05), `simulate_edits` (Fuel Comp; K ×1.05 ecu_reflash
      clear/active; Target AFR +4 on the target_afr grid) and `_simulate`
      with the 20260920 image.
    - **Gate.** The targeted tests pass, 638 plus 1 xfailed: the `test_sim_*`,
      `test_paired_*`, `test_rom_edit_spec`, `test_rom_editor*`,
      `test_rom_levers*` and `test_rom_sim_*` files, `test_rom_sim_world`,
      `test_ignition_timing`, `test_mcp_rom_editor` and
      `test_rom_library_api`. `test_layering`, `test_source_rules`,
      `test_script_cli_snapshot` and `test_exceptions_pickle` pass, 369.
      `test_rom_sim_cli.py` and `test_script_cli_snapshot.py` are unchanged.
      No touched path is in `scripts/ml_paths.txt`.
  - Must not change: the CLI's JSON output. After R30.2.

- [ ] **R30.6 mypy never loads 31 of the 113 `scripts/` and `tools/`
  modules.** `[tool.mypy] files = ["src", "tests"]`, on the theory that
  scripts are checked through the tests that import them. Thirty-one are
  imported by no test, including `scripts/check_dev_env.py`, which gates every
  push. Under the repo's strict config they produce 38 errors in 10 files:
  16 in a dead file (R30.7), 7 real ones in `scripts/timing_compare.py`
  (iterating a `Hashable`), six `import-not-found` / `unused-ignore` pairs
  from the `try: from _cli … except ImportError: from scripts._cli` dance,
  and four singletons.
  - [x] Add `scripts` and `tools` to `files` with `explicit_package_bases`
    and `namespace_packages`; fix `timing_compare`; give the `_cli` dance one
    targeted ignore. About 10 s more on a warm gate.
    **Done 2026-11-15.** `[tool.mypy]` now has `files = ["src", "tests",
    "scripts", "tools"]`, `namespace_packages = true`,
    `explicit_package_bases = true`; bare `mypy`: "Success: no issues found in
    1422 source files" (was 1310; +112 = every `scripts/` and `tools/`
    module). The tree had 22 errors in 9 files by then (the dead file is gone,
    R30.7). Fixed for real: `timing_compare` (`iterrows()` index is
    `Hashable`: one `cast` to the `(rpm_cell, load_cell)` interval pair, which
    also cleared six `has-type`); `obd_coverage` (`dict` -> `dict[str, Any]`);
    `catalog._scoped_config` (`config: CanaryConfig = load_config(args)`, the
    Any came from the unresolved `_cli`); `replay_window` (it read
    `config.alerts.anomalies_dir`, which does not exist: `AlertsConfig` has no
    such field, so omitting `--anomalies-dir` raised `AttributeError`; now
    `config.daemon.anomalies_dir`, the field every other reader uses, and the
    `--help` text says so. The one behaviour change: a crash became the
    documented default). The `_cli` dance: five scripts
    (`catalog`, `migrate_to_profiles`, `validate_command_pairing`,
    `inventory_behavior_inputs`, `sustained_validation`) had the ignore on the
    fallback import, so `import-not-found` fired on the first; moved to the
    first import as `# type: ignore[import-not-found]` and the fallback has
    none, the shape the other 32 scripts already use (so R30.8's mechanical
    change has one form to converge on). One ignore for the third-party stub:
    `classify_sessions` (`pq.ParquetFile(...).iter_batches` untyped in
    pyarrow, `[no-untyped-call, unused-ignore]` as everywhere else in `src`).
    Nothing left out of scope. Guard:
    `tests/test_source_rules.py::test_every_script_and_tool_is_type_checked`
    (every `.py` under `scripts/` and `tools/` lies under a `files` entry, no
    `exclude`, both package settings on) and its negative test
    `test_the_mypy_scope_check_names_a_file_it_would_miss`. Bare `mypy` wall
    time, this laptop: cold cache 94 s -> 134 s (+40 s, another agent was
    running); warm, nothing changed 0.6 s -> 0.8 s; first run after adding
    the 112 files onto a src/tests-warm cache 2.9 s.
  - [x] `[tool.ruff] target-version` is `py311`; `requires-python` and mypy
    say 3.12. Set `py312`, run one `ruff check --fix`, commit that alone.
    **Tried 2026-11-15, not applied.** `py312` turns on UP040, UP046 and
    UP047: 23 new findings in 16 files (3 `TypeAlias` -> `type`, 10
    `Generic[T]` classes, 10 generic functions; `src/yaml_load.py`,
    `src/jsonl_store.py`, `src/models/manager.py`, `src/collection/isotp.py`,
    `src/collection/reconnect.py`, `tests/waits.py`, ...). Every one is an
    unsafe fix, so plain `ruff check --fix` changes nothing; they are PEP 695
    rewrites of generics (`type` aliases evaluate lazily, `Generic[T]`
    classes change their MRO) across `src` files other sessions edit.
    Reverted to `py311`. Operator decision: (a) set `py312` and convert the 23
    by hand in its own reviewed commit, (b) set `py312` and `ignore =
    ["UP040", "UP046", "UP047"]` until a generics sweep, (c) leave `py311`.
    **Operator chose (b), 2026-11-15. Done:** `target-version = "py312"` and
    `ignore = ["UP040", "UP046", "UP047"]` in `[tool.ruff.lint]`, with the
    reason beside it. `ruff check .` under py312 adds no finding beyond the
    three ignored rules.
  - [ ] Generics sweep: convert the 23 UP040/UP046/UP047 sites by hand in one
    reviewed commit, then drop the `ignore`.

- [~] **R30.7 Dead or one-shot tools.**
  - `tools/decode_uprev_rom_research.py` (116 lines, untyped, untested, no
    reference anywhere) re-implements `src/analysis/osiris.py`: its `mask` is
    byte-identical, `read_sections` is `Container.parse`, and
    `decode_rom` / `encode_rom` are `transform(encode=)`. Delete it.
    **Done 2026-11-13: deleted** (tracked file). Grep of the whole tree
    (`src scripts tools tests docs .claude` incl. commands, skills, hooks and
    agents; systemd units and `install_*` scripts; `pyproject.toml`) for
    `decode_uprev_rom_research` found only this doc line, so no reference
    needed updating; `test_source_rules` (allowlists untouched),
    `test_script_imports` and `test_layering` still pass.
  - Operator question, each: `tools/watch_uprev_logs.py` (only an archived
    roadmap mentions it); `scripts/migrate_to_profiles.py` (a migration
    applied in July); `scripts/decode_flash.py` (440 lines, no test; its own
    docstring says it cannot decode this platform's flash channel, and
    `decode_uprev_flash.py` superseded it).
    **Not deleted (operator question stays open; checked 2026-11-13):**
    `scripts/decode_flash.py` is named as an operator procedure in
    `docs/data-architecture.md:312` (watching another tool own a diagnostic
    session: passive mode + `candump`, decoded by it) and in the docstring of
    `scripts/decode_uprev_flash.py:33`, and has a CLI snapshot entry in
    `tests/fixtures/script_cli_snapshot.json`; `scripts/migrate_to_profiles.py`
    is cited by `scripts/backfill_vehicle_ledger.sh:52` (ledger provenance
    text), has a `tests/test_source_rules.py` allowlist entry and a snapshot
    entry; `tools/watch_uprev_logs.py` has only archived-roadmap mentions
    (`docs/roadmap/archive/tuning-analysis-toolkit.md:255,258`, R06 archive)
    and its own docstring, but the item is an operator question, not an agent
    call. Remainder: operator keep/delete for these three; deleting any also
    shrinks the `test_source_rules.py` allowlist and the CLI snapshot.
  - Not dead: `tools/migrate_channel_names.py` (D03 still drives it).

- [ ] **R30.8 Test scaffolding for config, repo root and the app client is
  copied per file.**
  - 98 module-level functions returning a `CanaryConfig` in 85 files
    (R07.2 counted 59). Sixty-three files define their own `EXAMPLE_YAML`
    in five spellings though `tests/configs.py` exports it; six use a
    cwd-relative `Path("canary.example.yaml")`.
  - 176 test files recompute `Path(__file__).resolve().parents[1]`;
    `src.paths.REPO_ROOT` exists.
  - 144 `create_app(` calls in 49 files, 107 literal
    `control_token="secret"`, 16 per-file `_auth()` helpers, 20 `_client`
    builders of one shape.
  - [ ] Fixtures `example_config` and `repo_root`;
    `tests/app.py: make_app_client(tmp_path, *, token="secret", **kw)`.
  - [x] A shrink-only guard on new `EXAMPLE_YAML =` and `parents[1]`
    assignments in `tests/`. `tests/test_source_rules.py` rows
    `tests-redefine-example-yaml` (module-level, `tests/configs.py` exempt:
    60 files at HEAD) and `tests-recompute-repo-root`
    (`Path(__file__)[.resolve()].parents[1]`, multi-line spellings included:
    177 files / 183 hits at HEAD, `tests/configs.py` among them). Per-file
    counts, ratchet both ways.
  - Maintenance only: one config load is 4.6 ms.

- [ ] **R30.9 Fakes with a canonical home that tests do not use.**
  - Twelve hand-rolled fake clocks in twelve files;
    `src/validation/scripted_ecu.FakeClock` is the reference.
  - Eight `read_did` fakes in four files, five with `read_dids` batching.
    These pin "a gap in a batched response is re-read, never inferred"; each
    copy can quietly stop modelling it. `test_did_log_tool.FakeTransport` is
    the superset.
  - `_write_session` defined 18 times (R07.5 counted 16); two copies are
    identical.
  - Eleven files swap `DaemonClient._client`. It needs a `transport=`
    keyword on `mcp_server/client.py`.
  - [ ] `tests/fakes.py`: `FakeClock`, `FakeMode22Transport`;
    `tests/builders.write_session(root, name, channels=, n=, start=)`.

- [ ] **R30.10 The shell gates state one rule three ways and describe a gate
  that no longer exists.**
  - "Documentation only" is a case-glob in `git-hooks/pre-commit` and a
    regex in `pre-push` and `push.sh`; the two grammars differ.
  - `check.sh` runs bare `pytest -v` (slow tier included) under a header
    that says it runs "exactly what ci.yml runs". `pyproject`, `ci_gate.sh`
    and `ci.yml` each spell the tier selection.
  - `pre-push` says the gate takes "~7 min" (it is about one).
    `ci_gate.sh` prints "full vitest + e2e run in CI" while CI is off.
  - The pytest and vitest "re-run failures serially once" blocks in
    `ci_gate.sh` are the same fifteen lines twice. No test references any of
    these scripts.
  - [ ] `scripts/lib/doc_only.sh` sourced by all three; one tier string with a
    test that it equals `ci.yml`'s; fix the stale text; `check.sh` runs the
    push-gate tier unless `--full`.
  - Land with a `scripts/push.sh` run on a documentation-only and a code
    commit.

- [x] **R30.11 `scripts/install_pi.sh` is 1,058 linear lines with one
  265-line `if`.** Three functions; sections for hostname, wired link,
  GPS and time (the long conditional), journal, cloud-init, cgroup, HAT
  overlay, Node and the dashboard; fourteen heredocs. HAT and GNSS logic
  changes with each board revision, and each change edits the middle of that
  conditional. `tests/test_install_pi_script.py` (64 tests over
  `CANARY_INSTALL_ROOT`) already tests per section.
  - [x] `lib/install_gps_time.sh`, `lib/install_hat.sh`,
    `lib/install_network.sh`, sourced. Extend the list of libs for which
    `update_canary.sh` re-execs. Done 2026-11-12: `install_pi.sh` 1,058 ->
    542 lines; `install_network.sh` (`install_hostname`, `install_wired_link`),
    `install_hat.sh` (`hat_detect`, `hat_fact`, `hat_refuse_uart0_console`,
    `install_hat_overlay`), `install_gps_time.sh` (`gps_time_settings`,
    `install_gps_time` calling `gps_choose_device`, `gps_configure_gpsd`,
    `gps_configure_chrony`; the 265-line `if` is now the 17-line `install_gps_time`). Same
    commands, same order, no `local` (the moved code's variables stay global
    for the closing summary); `hat_detect` ends `return 0` because its last
    `[[ -r ]] &&` would trip `set -e` at the call once it is a function.
    `update_canary.sh`: the re-exec list takes the three libs, and
    `INSTALL_PI_INPUTS` takes `scripts/lib/install_*.sh` (without it a
    lib-only update would not run the installer at all); new
    `test_a_changed_installer_section_reexecs_and_runs_install_pi` (3 cases).
    shellcheck -x on `install_pi.sh`: clean.
  - [x] Acceptance: `test_install_pi_script.py` passes (75 passed,
    `pytest -m ""`). Its assertions are unchanged; the one fixture line that
    copied two named libs into the scratch checkout now copies
    `scripts/lib/*.sh`, or the new sections would not be there to source.
    Stronger: HEAD (a worktree) and the new script run under the same sandbox
    (the test's shims) for 28 configurations (no HAT, rev 2.0 / 2.1 / blank /
    unknown / foreign HAT, explicit and autodetected GPS device, GPS stack
    off, custom and refused baud/rate, uart0 console refusals, hostname
    explicit/MAC/refused, wired default/custom/none/edited/refused, data
    drive with and without, and a re-run of the first install): rc, stdout,
    stderr, shim command log and the whole file tree are identical once the
    random token, `mktemp` names and bash's own `line N:` prefix on one
    redirect-error message are normalized.

- [x] **R30.12 Guards that cost a few lines.**
  - `rust/` is outside every gate and its constants are hand-copied from
    Python (`X_SPLIT`, `AREA_TERM_SHIFT`, `PURGE_TERM_SHIFT`, … in
    `fuel_comp_load.rs`; `MAX_FLAG_BIT`, `MAX_CELL_TO_WORD_SHIFT` in
    `target_phi.rs`). Only the shared JSON vectors catch drift, and only when
    someone builds the wheel. A pure-Python test that reads the `pub const`
    lines and compares them with the Python attributes; record in E06 that
    the crate is ungated. E06.4 (adopt, defer or reject) is still open.
    **Done 2026-11-13:** `tests/test_ecu_twin_rust_constants.py` (25 tests):
    parses every `pub const` in `rust/ecu_twin_core/src/*.rs`, compares each
    with its Python attribute (`src.decode.fuel_comp_load`,
    `target_phi_command`, `ecm_lookup`, `src.analysis.ecu_state_models`) and
    `CONTRACT_VERSION` with `data/ecu_twin_contract.yaml`; a new unmapped Rust
    constant also fails. Checked it fails: `X_SPLIT 0x3C00 -> 0x3C01` in the
    Rust file and an added `pub const` each turned it red (reverted). E06.4
    now carries the "crate is ungated" note.
  - `tests/slow_tests.txt` (437 ids) has no staleness check: a renamed test
    silently rejoins the one-minute gate. A static test (file and function
    exist, sorted, no duplicates) naming `scripts/mark_slow_tests.py`. Zero
    stale ids today. That file has another session's edits; add the test
    after they land.
    **Done 2026-11-13:** `tests/test_slow_tests_list.py` (3 tests, AST-only:
    file exists, function exists in its class when named, sorted, no
    duplicates, plus a test of the checker itself). It found ONE stale id,
    contrary to "zero today":
    `test_rom_levers_parity.py::test_a_timing_trim_and_the_maf_curve_are_the_clis_alone`
    was renamed in `e8a669be` to `..._an_unmapped_byte_...`, so that 3.2 s
    test had been in the one-minute gate. Fixed by renaming that one id in
    `tests/slow_tests.txt` (one-line edit, timing kept; the file was clean).
  - The three example configs are about 75 % copies of `canary.example.yaml`
    (pi: 74 of 96 keys equal). A test that shared keys equal the base or sit
    on a named override list.
    **Done 2026-11-13:** `tests/test_example_configs.py` (4 tests): flattens
    each variant against `canary.example.yaml`; the set of shared keys whose
    value differs must equal the variant's named `OVERRIDES` set exactly (12
    for pi, 7 dev, 13 sim), so a stale override and an unlisted drift both fail.

- [~] **R30.13 Small duplicates.**
  - Three stdlib HTTP clients (`scripts/canaryctl.py`,
    `src/control/passive_client.py`, three `urlopen` sites in
    `scripts/canary_desk.py`) and two token resolvers:
    `canaryctl` honors `CANARY_CONTROL_TOKEN_FILE`, `passive_client` does
    not, so two tools can authenticate differently against one Pi. Grow
    `passive_client` into a stdlib-only `control/daemon_http.py`.
    **Partly done 2026-11-13:** the authentication split is fixed, the client
    merge is not. I took the "add the tiers" option: `passive_client.resolve_token`
    now has `canaryctl`'s exact precedence: `$CANARY_CONTROL_TOKEN`, then the
    `$CANARY_TOKEN` alias, then both names in `$CANARY_ENV_FILE` /
    `~/.canary/env` (its own small stdlib parser, minus canaryctl's
    permissions warning, because canaryctl cannot import `src`), then
    `path` / `$CANARY_CONTROL_TOKEN_FILE` / `/etc/canary/control.token`; a blank
    value at any tier falls through. `canaryctl` strips env and env-file
    values to match (a padded or whitespace-only token failed in one tool and
    not the other). `tests/test_daemon_http_token.py` (15 tests) asserts the
    same answer across that whole table, a whitespace-only env case, and the
    same parsed pairs from one tricky env file. Behaviour changes:
    `resolve_token(path)` now defaults to `None` (was
    `DEFAULT_TOKEN_PATH`; an explicit path still wins), a `UnicodeDecodeError`
    reading the token file now reads as no token, and a laptop with the token
    only in `~/.canary/env` now authenticates via `tools/_bus_tool.py`. NOT done: a shared `daemon_http.py`
    and the `canary_desk.py` `urlopen` sites. `canaryctl` imports nothing
    from `src`, and `import src.control.<anything>` runs `src/control/__init__`
    (capture controller, pandas via `storage.sessions`: measured 1.1 s) on
    every CLI call; sharing code needs `src.control` made lazy or a package
    outside it first.
  - `_base_url` is byte-identical in `canaryctl.py` and
    `companion/services.py`; `venv_python` in `install_deps.py` and
    `venv_launch.py`.
    **Not merged, now pinned equal (2026-11-13):** each pair sits on one side
    of a bootstrap/stdlib boundary (`canaryctl` for the reason above;
    `install_deps` / `venv_launch` run before any project import). Parity
    tests in `tests/test_daemon_http_token.py` (`_base_url` on 5 inputs;
    `venv_python` on linux/win32/darwin). Remainder: the merge itself.
  - Thirty-two of 57 scripts open with the same double `_cli` import list.
    Not attempted 2026-11-13: a 32-script mechanical change tied to R30.6's
    one-ignore decision for the same `try/except ImportError` dance; do it
    with R30.6.
  - Library functions living in scripts that tests import by name
    (`lambda_delivery_evidence.py`'s six checks, `timing_compare.py`): move
    one only when a second caller appears. `ups_shutdown_watch.py` stays
    stdlib-only on purpose.

The eight "JSON-safe" converters (`_jsonable`, `_json_default`) are R24.10.

## Looked at and fine

- `scripts/ci_gate.sh`'s worktree, slot and stamp design is intricate and
  each part answers a recorded incident. No defect found.
- `tests/conftest.py` (381 lines) is mostly hooks; three autouse fixtures.
- Sleeps: 31 literal sleeps of 0.2 s or more in 17 files; the long ones are
  hang guards. Nothing to add to T01.
- `tests/test_daemon.py` (2,655 lines) is 70 tests in about nine clusters
  with siblings already split off.
- `scripts/ml_changed.sh`, `restart_canary.sh`, `canup.sh`: small and
  single-purpose.

## Not reviewed

The ML entry scripts (R27 has `retrain` and `label_sessions`);
`update_canary.sh`, `sync-train.sh`, `data_drive.sh`, `setup_pi_dev.sh`,
`prepare_image.sh` internals; `firmware/usb-adc`; `hardware/`; individual
test assertions. Cargo, pytest and the gate were not run for this review.

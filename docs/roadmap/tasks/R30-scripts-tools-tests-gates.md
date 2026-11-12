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

- [ ] **R30.2 Tests import each other's private builders again.** R07.5
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
  - [ ] `tests/sim_builders.py`, `tests/target_phi_builders.py`,
    `tests/evidence_builders.py`; `scheduler`, `write_session` and
    `submit_twin` into `tests/builders.py`, under public names. `LAYERS` gets
    an importable home outside a test module.

- [ ] **R30.3 "Scope the config to the tooling profile" is spelled at about
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
  - [ ] `scoped_config_for_tooling(config, slug)` in `src/vehicle/profiles.py`
    (or wherever R26.4 puts path scoping); `_cli.scoped_config` re-exports it;
    delete the four `_scoped*` functions; migrate the direct callers.
  - [ ] A pandas-free `tools/_cli.py` with `add_config_profile_args` and
    `load_scoped`.
  - Pinned by `test_script_cli_snapshot.py`, `test_script_imports.py`,
    `test_bus_tool.py`.

- [ ] **R30.4 `--profile` means a slug in some tools and a directory in
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
  - [ ] One `add_profile_dir_args` that takes a slug, resolves `data/<slug>`
    through `vehicle_data_file` (so platform files are found), and takes the
    sessions root from the config or `--sessions`. No default slug.
  - Regenerate the CLI snapshot on purpose.

- [ ] **R30.5 `tools/rom_sim.py` runs a second copy of the ROM-edit
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
  - [ ] Lift the template builder, the kit / base / candidate loader and
    compare / trace into `src/analysis/`. The tool keeps argparse, thin
    `cmd_*` adapters and its "never write inside the checkout" policy.
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
  - [ ] Add `scripts` and `tools` to `files` with `explicit_package_bases`
    and `namespace_packages`; fix `timing_compare`; give the `_cli` dance one
    targeted ignore. About 10 s more on a warm gate.
  - [ ] `[tool.ruff] target-version` is `py311`; `requires-python` and mypy
    say 3.12. Set `py312`, run one `ruff check --fix`, commit that alone.

- [ ] **R30.7 Dead or one-shot tools.**
  - `tools/decode_uprev_rom_research.py` (116 lines, untyped, untested, no
    reference anywhere) re-implements `src/analysis/osiris.py`: its `mask` is
    byte-identical, `read_sections` is `Container.parse`, and
    `decode_rom` / `encode_rom` are `transform(encode=)`. Delete it.
  - Operator question, each: `tools/watch_uprev_logs.py` (only an archived
    roadmap mentions it); `scripts/migrate_to_profiles.py` (a migration
    applied in July); `scripts/decode_flash.py` (440 lines, no test; its own
    docstring says it cannot decode this platform's flash channel, and
    `decode_uprev_flash.py` superseded it).
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

- [ ] **R30.11 `scripts/install_pi.sh` is 1,058 linear lines with one
  265-line `if`.** Three functions; sections for hostname, wired link,
  GPS and time (the long conditional), journal, cloud-init, cgroup, HAT
  overlay, Node and the dashboard; fourteen heredocs. HAT and GNSS logic
  changes with each board revision, and each change edits the middle of that
  conditional. `tests/test_install_pi_script.py` (64 tests over
  `CANARY_INSTALL_ROOT`) already tests per section.
  - [ ] `lib/install_gps_time.sh`, `lib/install_hat.sh`,
    `lib/install_network.sh`, sourced. Extend the list of libs for which
    `update_canary.sh` re-execs.
  - Acceptance: `test_install_pi_script.py` passes unchanged.

- [ ] **R30.12 Guards that cost a few lines.**
  - `rust/` is outside every gate and its constants are hand-copied from
    Python (`X_SPLIT`, `AREA_TERM_SHIFT`, `PURGE_TERM_SHIFT`, … in
    `fuel_comp_load.rs`; `MAX_FLAG_BIT`, `MAX_CELL_TO_WORD_SHIFT` in
    `target_phi.rs`). Only the shared JSON vectors catch drift, and only when
    someone builds the wheel. A pure-Python test that reads the `pub const`
    lines and compares them with the Python attributes; record in E06 that
    the crate is ungated. E06.4 (adopt, defer or reject) is still open.
  - `tests/slow_tests.txt` (437 ids) has no staleness check: a renamed test
    silently rejoins the one-minute gate. A static test (file and function
    exist, sorted, no duplicates) naming `scripts/mark_slow_tests.py`. Zero
    stale ids today. That file has another session's edits; add the test
    after they land.
  - The three example configs are about 75 % copies of `canary.example.yaml`
    (pi: 74 of 96 keys equal). A test that shared keys equal the base or sit
    on a named override list.

- [ ] **R30.13 Small duplicates.**
  - Three stdlib HTTP clients (`scripts/canaryctl.py`,
    `src/control/passive_client.py`, three `urlopen` sites in
    `scripts/canary_desk.py`) and two token resolvers:
    `canaryctl` honors `CANARY_CONTROL_TOKEN_FILE`, `passive_client` does
    not, so two tools can authenticate differently against one Pi. Grow
    `passive_client` into a stdlib-only `control/daemon_http.py`.
  - `_base_url` is byte-identical in `canaryctl.py` and
    `companion/services.py`; `venv_python` in `install_deps.py` and
    `venv_launch.py`.
  - Thirty-two of 57 scripts open with the same double `_cli` import list.
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

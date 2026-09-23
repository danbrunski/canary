# S02 — the Pi updater (`canary-update`)

Opened 2026-09-22, after four runs of `scripts/update_canary.sh` (installed as
`/usr/local/sbin/canary-update` by `install_pi.sh`, first added in `a85411c`).
Same conventions as D03: `[x]` only with the evidence linked, `[~]` partial
with the rest named.

## Where it stands

It works, and the parts that protect a drive already hold. It refuses while a
capture is active (through `restart_canary.sh --check`), refuses a checkout
with tracked local changes, and prints `source: <before> -> <after>`, which
`.claude/commands/drive.md` treats as the deploy's hash check. It restarts
only through `restart_canary.sh`. Since `119527d` it waits 90 s for `/status`
(it used to report a healthy 24 s start as a failure), then checks that
`canary.service`'s cgroup `memory.max` exists and says so loudly when the
limits are not enforced.

What 2026-09-22 showed is still rough:

- A run that changes `update_canary.sh` itself executes the **old** copy to
  the end: `install_pi.sh` replaces the file mid-run, and bash keeps the old
  one open. The first run after `119527d` printed the old 20 s "did not
  answer" failure while the daemon was healthy.
- Every run does everything: `pnpm install --frozen-lockfile && pnpm build`, a
  full `pip install -e .`, and `install_pi.sh`'s unconditional restarts of
  gpsd, chrony and journald (the daemon logs `gpsd collector failed;
  reconnecting` on every update). A docs-only update costs minutes and a GPS
  gap.
- A failed health check leaves the new code installed and the daemon down or
  broken, with nothing but an exit code.
- `install_pi.sh` prints "Set 'hardware.gps.source: gpsd' in
  /etc/canary/canary.yaml to use it." on every run, including on the Pi,
  whose config already says `source: gpsd`.
- None of it has a test. Every behaviour above was found by running it on the
  Pi.

## Rules

- The refusals stay first and unconditional: an active capture, tracked local
  changes. Nothing here may add a path that restarts the daemon without
  `restart_canary.sh`.
- `source: <before> -> <after>` stays, on its own line, in that form —
  `drive.md` reads it. If the output changes, update `drive.md` in the same
  commit.
- "Skip unchanged work" never means "skip because it looks fine": the
  decision is made from `git diff --name-only <before> <after>` and from the
  artefact's presence, and every skip is printed.
- Changes to `install_pi.sh` must keep it safe to run on a fresh card (the
  first-install path), not only on the dev Pi.

---

## Desk

- [x] **S02.1 Put the updater under test first.** Everything below changes
  a script nobody can run off the Pi today. **Do:** a pytest module that
  builds a throwaway git "origin" and checkout in `tmp_path`, puts shims for
  `systemctl`, `curl`, `pnpm`, `runuser`, `restart_canary.sh` and
  `install_pi.sh` on `PATH` (each logging its argv to a file and exiting with a
  scripted code), and runs `update_canary.sh` with `CANARY_HOME` pointed at the
  checkout. Needs a way to run without root: an env override for the
  `EUID` check that only the tests set. **Accept:** tests for today's
  behaviour first: refusal on an active capture, refusal on a dirty tracked
  tree, the `source:` line, the 90 s health loop (with a shimmed `curl` that
  succeeds on the Nth call and a shortened `CANARY_HEALTH_WAIT_SECONDS`), and
  the memory-limit warning when the cgroup file is absent (make the cgroup path
  overridable for the test). Then each item below adds its own.
  **Evidence (2026-09-22):** `tests/test_update_canary_script.py` (28 tests,
  `pytest tests/test_update_canary_script.py -n 4` → 28 passed in 8 s). It
  builds a git upstream + clone in `tmp_path`, shims `systemctl`, `sudo`,
  `curl`, `pnpm`, `runuser` on `PATH` and `.venv/bin/python` in the checkout
  (each logs argv, exit scripted by env), commits a logging `install_pi.sh`
  shim to the fake upstream, keeps the *real* `restart_canary.sh` so the
  capture refusal is the real one, and launches the updater from a copy
  outside the checkout as `/usr/local/sbin/canary-update` is. Root override
  `CANARY_UPDATE_ALLOW_UNPRIVILEGED=1`; cgroup path `CANARY_CGROUP_DIR`;
  poll `CANARY_HEALTH_POLL_SECONDS`. Today's behaviour:
  `test_refuses_while_a_capture_is_active` (exit 3, no fetch),
  `test_refuses_a_checkout_with_tracked_local_changes` (exit 4),
  `test_prints_the_source_line_in_drive_md_form`,
  `test_health_loop_waits_for_the_nth_curl_success` (3 failures then the
  answer: 4 polls), `test_health_loop_gives_up_after_the_wait`,
  `test_warns_when_the_memory_cgroup_file_is_absent`,
  `test_reports_the_memory_limits_when_enforced`,
  `test_fetch_failure_changes_nothing` (exit 5).

- [x] **S02.2 Re-exec the new updater when the update changes it.** After the
  fetch, if `git diff --name-only <before> <after>` includes
  `scripts/update_canary.sh`, `exec` the checkout's new copy with the same
  arguments and an environment marker (`CANARY_UPDATE_REEXEC=<after>`) so it
  skips the fetch and cannot loop. Print that it did. **Accept:** a test where
  the "after" commit changes the script's health message, and the run prints
  the new message; a test that the marker prevents a second re-exec.
  **Evidence:** `update_canary.sh` execs `${CANARY_HOME}/scripts/update_canary.sh
  "$@"` with `CANARY_UPDATE_REEXEC=<after>`, `CANARY_UPDATE_REEXEC_BEFORE=<before>`
  and `CANARY_UPDATE_STARTED_AT` when the diff names itself; the marked copy
  skips the fetch, keeps the refusals, prints `re-executed from <after>`.
  `test_reexecs_the_new_updater_when_the_update_changes_it` (the after commit
  changes the completion message; the run prints `[NEW UPDATER]`, one git
  fetch, one restart) and `test_reexec_marker_prevents_a_second_reexec`.

- [~] **S02.3 Skip the work the diff doesn't touch.** From `git diff
  --name-only <before> <after>`:
  - Dashboard build: only if anything under `src/dashboard/static/` changed,
    or `dist/` is missing, or older than the newest source file there. A
    stale gitignored `dist` has misled a drive before (a panel that looked
    empty on the Pi was an old build, not missing code), so "missing or
    stale" must rebuild even with no diff.
  - `pip install -e .`: only if `pyproject.toml` (or a lock/requirements file,
    if one is added) changed, or `pip check` fails.
  - `install_pi.sh`: only if it, anything under `systemd/`, or the files it
    installs changed. When `before == after`, say "already at <hash>" and do
    only the restart the operator asked for.
  - `--full` forces every step, and is what a first install and a
    "something's wrong" run use.
  Print one line per step: `ran` or `skipped (<reason>)`. **Accept:** tests
  for a docs-only diff (no build, no pip, no install, one restart), a
  dashboard-only diff, a `pyproject.toml` diff, a missing `dist/`, and
  `--full`. On the Pi, time a docs-only update before and after and record
  both here.
  **Evidence:** `apply_steps` decides from `git diff --name-only` + artefacts
  (`decide_dashboard`/`decide_pip`/`decide_install`), prints `ran (<reason>)`
  or `skipped (<reason>)` per step, `already at <hash>` when before==after.
  Tests: `test_docs_only_update_skips_build_pip_and_install` (one restart),
  `test_dashboard_only_update_builds_only_the_dashboard`,
  `test_pyproject_change_runs_pip_only`, `test_failing_pip_check_runs_pip_without_a_diff`,
  `test_missing_dist_rebuilds_even_with_no_diff`, `test_stale_dist_rebuilds_even_with_no_diff`,
  `test_systemd_change_runs_install_pi_which_covers_pip`, `test_full_forces_every_step`,
  `test_every_skip_is_printed_once_per_step`. **Remaining:** the Pi timing of
  a docs-only update before/after (needs the Pi; a peer session holds it
  2026-09-22).

- [~] **S02.4 `install_pi.sh` restarts a service only when its config changed.**
  It restarts gpsd (`systemctl restart gpsd.socket gpsd.service`), chrony and
  journald unconditionally, so every update drops GPS for a reconnect cycle.
  **Do:** restart each only when the file the script writes for it changed
  (compare before writing, or checksum), and on the first install. The
  canary units' `daemon-reload` stays unconditional; it is cheap and restarts
  nothing. **Accept:** a second `install_pi.sh` run on an unchanged system
  restarts nothing and says so; on the Pi, no `gpsd collector failed;
  reconnecting` line in the daemon log across an update that did not touch
  the GPS config.
  **Evidence (desk, 2026-09-22):** `install_pi.sh` writes each managed file
  through `write_managed <service> <dest>` (stage to a temp file, `cmp`
  against the installed one, `install -m 0644` only on a difference; an absent
  file — first install — counts as one) and restarts through
  `restart_if_changed`: gpsd on `/etc/default/gpsd`, `70-canary-gps.rules` or
  `canary-gpsd@.service`; chrony on `conf.d/canary-gps.conf` or an added
  `confdir` include; `systemd-journald` on `90-canary-persistent.conf`.
  Otherwise it prints `unchanged: <service> not restarted` and runs `systemctl
  start` (a no-op for a running unit). `udevadm trigger` runs only with a gpsd
  restart; `daemon-reload` and `journalctl --flush` stay unconditional. Tests
  in `tests/test_install_pi_script.py` run the real installer unprivileged
  against a scratch root (`CANARY_INSTALL_ROOT`,
  `CANARY_INSTALL_ALLOW_UNPRIVILEGED=1`, test-only) with host commands shimmed:
  `test_first_install_restarts_gpsd_chrony_and_journald`,
  `test_second_run_on_an_unchanged_system_restarts_nothing` (no `systemctl
  restart`, three `unchanged:` lines, daemon-reload still called, no udev
  trigger), `test_a_changed_file_restarts_only_its_own_service` (5 cases),
  `test_a_new_gps_device_rewrites_the_rule_and_restarts_gpsd`,
  `test_adding_the_chrony_confdir_include_restarts_chrony`. 13 passed; before
  the change all 13 failed, and making `restart_if_changed` unconditional
  fails 8 of them. **Remaining:** on the Pi, no `gpsd collector failed;
  reconnecting` in the daemon log across an update that did not touch the GPS
  config (needs the Pi).

- [~] **S02.5 Roll back when the new code does not come up.** Record
  `<before>` before the fetch. If the health check fails, or the restart
  itself fails: fast-forward is undone with `git -C "$CANARY_HOME" reset
  --hard <before>` (safe here, because the updater already refused a dirty
  tracked tree), redo the steps S02.3 would run for `<after> -> <before>`,
  restart through `restart_canary.sh`, and re-check health. Print both
  outcomes, and exit non-zero with a distinct code whether or not the rollback
  succeeded (see S02.7). `--no-rollback` leaves the failed version in place for
  debugging. Never roll back past a commit the operator deployed by hand (the
  target is always the recorded `<before>`, nothing older). **Accept:** tests
  for a failing health check → rolled back and healthy; rollback also failing
  → says so, different exit code; `--no-rollback`. Before trusting it on the
  Pi, one driveway run against a deliberately broken ref
  (`CANARY_UPDATE_REF=<branch with a syntax error in src/main.py>`), parked, no
  capture active — record the output here.
  **Evidence:** on restart failure, a failed deploy step, or no `/status`
  within the wait: `runuser -u canary -- git reset --hard <before>` (the
  recorded before, never older), `apply_steps <after> <before>`, restart via
  `restart_canary.sh`, re-check. Exit 6 rolled back + healthy, 7 rollback
  failed, 8 `--no-rollback` / nothing older. Tests:
  `test_failed_health_rolls_back_to_before_and_is_healthy` (HEAD==before, the
  rollback redid the dashboard build, 2 restarts, exit 6),
  `test_rollback_that_also_fails_says_so_with_its_own_code` (exit 7),
  `test_no_rollback_leaves_the_failed_version_in_place` (exit 8, HEAD==after),
  `test_failed_restart_with_nothing_older_does_not_reset`,
  `test_failed_deploy_step_rolls_back_before_restarting` (no restart of code
  whose build failed). **Remaining:** the driveway run against a broken
  `CANARY_UPDATE_REF` (needs the truck, parked, no capture) — record the
  output here.

- [~] **S02.6 Say only what is true about GPS.** The "Set
  'hardware.gps.source: gpsd'" line is printed after configuring gpsd,
  whatever the config says. **Do:** read `hardware.gps.source` from
  `${CANARY_CONFIG}` (with the venv's Python and the project's own config
  loader, not a `grep`), and print the hint only when it is not already
  `gpsd`. **Accept:** a test for both cases; the Pi's update output no longer
  shows it.
  **Evidence (desk, 2026-09-22):** after `install_deps.py` (so the venv
  exists on a fresh card), when the GPS stack was installed, `install_pi.sh`
  runs `runuser -u canary -- env PYTHONDONTWRITEBYTECODE=1
  .venv/bin/python -c '...CanaryConfig.from_yaml(...).hardware.gps.source'`
  on `${CANARY_CONFIG}` and prints the `hardware.gps.source: gpsd` hint only
  when the value is not `gpsd`, naming the value; an unreadable config prints
  `could not read hardware.gps.source from <path> (<error>)` instead. Tests
  (real loader via a `src` symlink): `test_no_gps_hint_when_the_config_already_says_gpsd`
  (first install copies `canary.pi.example.yaml`, `source: gpsd`),
  `test_gps_hint_when_the_config_says_serial`,
  `test_gps_hint_when_the_config_cannot_be_read`,
  `test_no_gps_lines_when_the_gps_stack_is_skipped`. Making the hint
  unconditional fails the first. **Remaining:** the Pi's update output no
  longer shows it (needs the Pi, on the next update that runs `install_pi.sh`).

- [x] **S02.7 One summary, and exit codes that say what happened.** End every
  run with one block: `<before> -> <after>`, each step's `ran`/`skipped`, the
  restart result, health (with seconds to answer), memory limits enforced or
  not, rollback if any, and the total time. Distinct exit codes, documented in
  `usage()` and `docs/pi-setup.md`: refused (active capture), refused (dirty
  tree), fetch failed, health failed and rolled back, health failed and
  rollback failed. The capture refusal already exits 3 from
  `restart_canary.sh`; keep that value. **Accept:** the S02.1 tests assert the
  codes; `drive.md`'s deploy bullet names the summary instead of scraping
  lines, updated in the same commit.
  **Evidence:** `finish()` prints `=== canary-update summary ===` with
  `source:`, each step, `restart:`, `health: ok after <n> s`, `memory limits:`,
  `rollback:`, `result: ... (exit <code>)`, `total: <n> s` on every exit past
  argument parsing. Codes 3 (kept from `restart_canary.sh`), 4, 5, 6, 7, 8 in
  `usage()` and a table in `docs/pi-setup.md`; `drive.md`'s deploy bullet now
  names the summary block and the codes; README updated. Tests:
  `test_summary_block_names_every_outcome` (line order),
  `test_usage_documents_the_exit_codes`, `test_unknown_argument_is_a_usage_error`,
  `test_docs_describe_the_same_exit_codes`, plus every refusal/rollback test
  asserting its code.

- [~] **S02.8 Decide whether unenforced memory limits fail the update.**
  Today they are a warning. The limits exist because the digest once wedged
  the daemon for 38 minutes (D03.6), and they went unenforced for four days
  while `systemctl show` said otherwise. **Options:** (a) keep the warning;
  (b) fail the update, with exit code and message, unless
  `--allow-unenforced-memory`; (c) warn, and have the daemon itself report
  `memory_limits_enforced: false` in `/status` so a drive's health line
  shows it. (c) is the one that cannot be missed, since every drive reads
  `/status`, and it covers a Pi that was never updated through this script.
  **Accept:** the choice recorded here with the reason, and implemented with a
  test.
  **Decision (operator, 2026-09-22): (c).** The updater keeps its warning
  (under test: `test_warns_when_the_memory_cgroup_file_is_absent`, and the
  summary block carries `memory limits: NOT enforced (...)`); the daemon
  additionally reports `memory_limits_enforced: bool | null` in `/status`,
  read from `/sys/fs/cgroup/.../memory.max` (never `systemctl show`, per
  D03.21), so every drive's health line shows it and a Pi never updated
  through this script is covered too.
  **Done (2026-09-22, drive session):** `memory_limit_status()` in
  `src/models/health.py` reads the daemon's own cgroup (via `/proc/self/cgroup`,
  so it follows the unit wherever it runs) and `/status` `health.system` carries
  `memory_limits_enforced` (`true` / `false` for an explicit `max` / `null` for
  unknown) with `memory_max_bytes` and the path it read. Four tests cover
  enforced, `max`, absent and unreadable. `.claude/commands/drive.md` reads it
  in both the boot check and the per-wake-up health line.
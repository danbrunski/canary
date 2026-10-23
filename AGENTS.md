# CANary — agent instructions

Quick orientation for AI agents working in this repo, regardless of
which tool/harness is running them. Project README and
`docs/CANary-overview.md` are the human-facing entry points; this file
is the agent-facing one.

## What this is (and isn't)

CANary is a **hobby / research project**: a vehicle telemetry,
anomaly-detection, and diagnostics platform mounted on a Raspberry Pi 5.
It logs telemetry, learns "normal" operation with unsupervised models,
surfaces alerts on a local dashboard, and doubles as an **engine
performance analysis and diagnostic workbench** (PID/DID exploration,
learned behavior surfaces, physics-derived channels, trouble codes,
trip/session analysis).

The current install is one operator and one vehicle — a 2008 Nissan
Xterra, which serves as the development vehicle — but the design intent
is **generalized and adaptable to any vehicle**: per-VIN vehicle
profiles already partition all learned state, and vehicle-specific
facts belong in profile/config/data files, never hard-coded. Don't bake
Xterra-isms into shared code paths.

**This is a single-operator research rig, not a fleet product.** Wrong
inference, a stalled daemon, a missing anomaly event, a deleted session
directory — none of these matter to anyone but the operator, who is also
the only person in the vehicle.

**Transmitting on the CAN bus is normal and required — don't hedge about
it.** Every OBD/UDS request is a bus write. Polling telemetry, ISO-TP
flow control, DiagnosticSessionControl (0x10), SecurityAccess (0x27) and
ReadMemoryByAddress (0x23) to command a memory dump are all ordinary
CANary operations. There is no service allowlist on the active Canable
path: `CanObdClient.query()` and `POST /control/obd_request` build and
send whatever service byte the caller asks for. An earlier `read-only`
requirement (NFR-06) was written to mean "don't brick the ECU" and got
read literally as "never transmit," which blocked routine work; it has
been removed.

**The line that actually matters: nothing writes the ECU's non-volatile
memory without the operator's explicit permission, and never while the
vehicle is moving or the engine is running.** A reflash or calibration write
is the one operation here whose failure mode is an unrecoverable module, so
it gets two conditions and both always hold:

- **Explicit permission, per operation.** The operator asks for that write,
  in those words, at that time. An agent never starts one on its own
  initiative, never infers permission from a broader instruction ("tune it",
  "fix the fuelling", "deploy"), and never carries permission from one write
  to the next. If it is not clear that a non-volatile write was requested,
  it was not.
- **Key on, engine off, vehicle stationary.** Read `vehicle_state` first: a
  write proceeds only from `ignition_only`. `engine_running` refuses, and so
  does anything that cannot be classified. An interrupted flash on a parked
  truck is a recovery job; on a moving one it is an engine shutting off in
  traffic.

Reflashing is a direction this project is moving in, not something it
forbids (operator decision 2026-09-21, replacing the earlier blanket rule
that `canary` never writes non-volatile memory). Reads of any depth remain
fine, and so do volatile, reversible writes (clear DTC, actuation tests,
session control) — those need neither condition. That covers **every module
on the vehicle, not only the ECM**: reading and clearing codes on the airbag
(SRS), ABS, BCM or any other module, running their active tests, and
observing a scan tool do the same so CANary can learn it, are routine
diagnostic work (operator, 2026-10-04). Don't hedge about them. Bench-ECU groundwork is in
`docs/skunkworks/ecu-rw-bench-research.md`.

Two narrower cases, neither of them the policy above: the broadcast
capture path (`can_collector`) is a pure sniffer with no send path at
all, and the **legacy ELM327 path** (`obd_collector.py`) still refuses
Mode 04/0x2E/0x2F via `WRITE_COMMAND_MODES` / `assert_read_only_command`.
That legacy guard is now *stricter than NFR-06 requires* — none of those
three touch non-volatile memory — and survives only because nothing
needed it lifted yet. Lift it when the ELM327 path needs them; it has nothing
to do with the non-volatile-write conditions above.

**Implications for how to work:**

- Don't refuse or stall on actions because the operator is "in the
  vehicle" or "driving." They are typically the only stakeholder, and
  may be sitting in the driveway with the engine running just to
  generate live data for you to debug against. Treat that as the
  default development environment, not an emergency.
- Don't suggest staging-environment workflows, blue-green deploys,
  paged-on-call procedures, or PR review processes. Commit to `main`,
  push, the operator pulls on the Pi.
- Don't over-engineer for failure modes the system can't actually have.
  The detectors firing 200×/sec at saturation severity is annoying, not
  dangerous — don't add runtime kill-switches; fix the model.

## Check `vehicle_state` before drawing conclusions

`GET /status.vehicle_state` (also in the MCP `get_status` payload) is a
derived classification of what the daemon is actually seeing right now:

- `engine_running` — OBD fresh + RPM ≥ 400. Truck is in normal mode.
- `ignition_only` — OBD fresh + RPM == 0. KOEO; ECM responds but engine off.
- `engine_off` — CAN fresh, OBD stale. Plugged in, key out, on a vehicle
  whose bus stays awake. On the Xterra it lasts only seconds after key-off.
- `parked` — neither bus fresh, but the HAT's 12 V sense reads a battery
  (`supply_voltage`). The bus is asleep: key off, plugged in. A parked
  Xterra spends its nights here. With `collection.obd_sleeps_with_bus` (on
  for the Xterra) the OBD collector and the startup VIN probe do not
  transmit while parked, so they cannot wake modules. Polling resumes when
  any module talks.
- `bench` — neither bus fresh and the 12 V sense reads nothing: the Pi runs
  from USB-C or the UPS with no vehicle connected, the normal desk setup.
  Not a fault. The OBD collector waits instead of transmitting into an empty
  bus.
- `no_data` — neither bus fresh and no usable 12 V reading: no 12 V sense
  configured, or a reading that explains nothing. Before `parked` existed (2026-10-04) a parked truck read
  `no_data` too.
- `replay` — runtime replay is active; the "OBD"/"CAN" you see is
  re-emitted from a stored session.
- `passive` — CANary is not transmitting (passive armed, or another tester
  holds the ECM session), so stale OBD means "not asking", not "engine off";
  read `can_freshness_seconds`. `passive: true` rides on every verdict.
- `unknown` — the rule table didn't classify it cleanly; trust the raw
  fields.

**Always read this field before concluding anything about detector
behavior.** "IF and LSTM aren't firing" reads very differently when
`state` is `engine_off` (expected) vs `engine_running` (a real bug),
and again differently when `state` is `replay` (you're testing, not
seeing the truck). The classifier returns the raw signals — `rpm`,
`coolant_temp`, `run_time`, `obd_freshness_seconds`,
`can_freshness_seconds` — alongside the verdict so you can second-guess.

For controlled detector evaluation, prefer the replay path
(`start_replay`) over live telemetry: live data has its own
degradations independent of the model, so replay a known-good session
before concluding "detector X regressed."

## The control surface (MCP + HTTP)

`canary-mcp` (port 8001 on the Pi) exposes the live daemon to MCP
clients. `docs/mcp-server.md` is the source of truth. Useful when
debugging in-vehicle:

- `get_status` — daemon snapshot incl. replay state
- `get_recent_anomalies` / `search_anomaly_log` — what's firing
- `get_behavior_live` — per-model expected/actual/deviation
- `get_dataset_health` — corpus readiness, gaps, per-mode counts
- `query_telemetry` — DuckDB over the Parquet ring buffer
- `start_replay` / `stop_replay` — run a stored session through the
  live pipeline with isolated outputs (`_replay-*` paths). Use this
  for end-to-end evaluation without the truck.

Every MCP tool has an HTTP counterpart on the daemon itself
(`http://canary.local:8000`, `Authorization: Bearer
$CANARY_CONTROL_TOKEN`, routes under `/control/...`, `/anomalies`,
etc. — see `docs/headless-api.md`). If the MCP session dies, fall back
to direct HTTP.

## Project conventions

- **Python**: always `.venv/bin/python` (3.12), never system `python3`.
- **YAML**: load it with `src.yaml_load.load_yaml` (libyaml's parser, 8–10×
  the pure-Python one), never `yaml.safe_load`; `tests/test_yaml_load.py`
  refuses a direct load anywhere in `src/`, `scripts/`, `tools/` or `tests/`.
- **Branching**: work directly on `main`, no feature branches or PRs; push
  with `scripts/push.sh` (see "Push with" below).
- **GitHub CI is switched off** (operator, 2026-10-13). Both workflows
  (`ci.yml`, `nightly-slow.yml`) are disabled on GitHub because the account's
  Actions billing refused every run from 2026-10-08. The files stay in the
  repo; `gh workflow enable CI` and `gh workflow enable "Nightly slow tier"`
  turn them back on. Until then **the local push gate is the only gate**:
  don't `gh run watch` after a push, and don't treat the last red runs as a
  red `main`.
- **Lint/test gate: about a minute locally; the slow tier and full frontend
  checks by hand** (operator, 2026-09-24). A pre-commit hook runs ruff + mypy.
  The push gate (`scripts/push.sh`, below) runs lint, types and every test
  except the `slow` tier. Nothing runs the rest for you while CI is off: after
  changing code they cover, run `pytest -m slow` (the tier the nightly ran),
  and for dashboard work `pnpm lint`, `pnpm test` and `pnpm e2e` in
  `src/dashboard/static` (what per-push CI ran). A red slow test is fixed like
  a red main. When CI is on it runs lint and types once and splits pytest
  across 4 parallel jobs (`CANARY_TEST_SHARD=i/4`, a stable node-id hash in
  `tests/conftest.py`). While iterating, run the test files you are working on. For dashboard
  changes, `pnpm typecheck`, `pnpm format`, `pnpm lint` and the vitest files you
  touched (re-run `TuningFeedback.test.ts` once on flake before treating it as a
  regression). ESLint is type-aware, and its pre-existing violations are
  bulk-suppressed in `eslint-suppressions.json`. A new violation fails, and
  so does a fixed one until you run `pnpm lint:prune`. Fix a new violation;
  never re-run `--suppress-all` to hide it.
- **CI is the same gate, on the same versions.** CI installs
  `constraints-dev.txt`, the exact dev toolchain, and `scripts/check.sh`
  refuses to run on a venv that has drifted from it (`scripts/check_dev_env.py`
  names the packages). Unpinned CI failed twice on code the laptop passed:
  pandas-stubs on 2026-09-19, mypy 2.x + numpy 2.5 stubs on 2026-09-23. To
  upgrade a tool, follow `scripts/lock_dev_constraints.sh` and commit the new
  lock with whatever fixes it needs, never a lock the gate has not passed on.
  **CI does not install torch** (operator decision, 2026-09-23). Torch tests
  `importorskip` there and run only in the local `scripts/ci_gate.sh`, whose
  venv has torch. So a test must skip cleanly without torch, and a
  torch-dependent verdict comes from the local gate, never from CI. Don't
  propose adding torch to CI.
- **Push with `scripts/push.sh`.** It runs `scripts/ci_gate.sh` on the exact
  commit being pushed, in a worktree of its own, so another session's
  uncommitted edits cannot decide the verdict: ruff, format, mypy (with a
  cache kept between gates), and pytest without the `slow` tier — about a
  minute. The worktrees are kept (`.git/ci-gate/worktrees/<slot>`, one per
  concurrent gate, reset and cleaned each time) so their bytecode stays warm;
  a fresh tree spent 11 s of every gate recompiling. `slow` is every test measured at 1.5 s or more (operator, 2026-10-03)
  (`tests/slow_tests.txt`, regenerated by `scripts/mark_slow_tests.py`):
  ~365 tests, 5 % of the suite and most of its time. It adds the `ml_training`
  tier only when the push touches `scripts/ml_paths.txt` (see "Test tiers"):
  CI has no torch, so those pushes take minutes. When the push touches
  `src/dashboard/static` it runs pnpm typecheck, ESLint and Prettier on the
  changed files, and `vitest related`; full lint, vitest and e2e are yours to
  run while CI is off. A green run stamps that commit, and
  the tracked `pre-push` hook (`git config core.hooksPath scripts/git-hooks`)
  refuses any unstamped, non-documentation push. The gate cannot run inside
  the hook itself: git opens the connection first, and GitHub closed it during
  the gate on 2026-09-23. Tests that fail in the parallel run are re-run once
  serially. Failing again blocks the push. Passing is printed as "FLAKY UNDER
  LOAD" and belongs in `docs/roadmap/tasks/T01-test-suite-reliability.md`.
- **After pushing, confirm CI — when CI is on.** `gh run watch` (or
  `gh run list --limit 3`), and on a red run `gh run view <id> --log-failed`.
  A red `main` is fixed before anything else is pushed. If a pushed test
  flaked, re-run it alone before calling it a flake, and open a task for the
  flake either way. While CI is off, the push gate's verdict is the record.
- **Test tiers** (pytest runs in parallel via `-n auto`, needs
  `pytest-xdist` from the `dev` extra): bare `pytest` is the full suite —
  everything except `integration` and `ml_training`, about 5 minutes on a
  quiet laptop. Per-push CI and the push gate run its non-`slow` part. The
  `slow` part runs nightly.
  **The torch training / bake-off / FP-benchmark suites (`ml_training`)
  are not a commit gate** (operator, 2026-09-23): `scripts/ci_gate.sh` and
  CI run them only when a change touches a path in `scripts/ml_paths.txt`
  (`scripts/ml_changed.sh <base>` decides). Run them by hand with
  `pytest -m ml_training` when you change model or preprocessing code.
  While *iterating*, run the files you are working on, or the whole
  inner-loop tier `pytest -m "not integration and not slow"` (~1.5 min, the
  push gate's own suite). `-m integration` / `-m slow` /
  `-m ml_training` run those tiers explicitly (a command-line `-m`
  replaces the default); add `-n0` to force serial output when debugging
  one test.
- **Pi deployment path**: `ssh canary@192.168.137.2` (or
  `canary.local`); install via `sudo bash scripts/install_pi.sh`; data
  lives under `/data/canary/`. `/data` is on a USB NVMe on the dev Pi since
  2026-10-10. It is optional: `install_pi.sh` asks where `/data` goes (a
  drive by default when fitted), and with a drive the services refuse to
  start without it rather than write to the SD card (`scripts/data_drive.sh`,
  docs/pi-setup.md "Data Drive"). Control token at
  `/etc/canary/control.token`. **Nothing the Pi writes lives in the
  checkout** (S03): `daemon.state_dir: /data/canary` puts models (manifests
  and `behavior_models.yaml` included) under `/data/canary/models/<vehicle>/`
  and every ledger, job and override under `/data/canary/control/`. Models are
  not in git at all — they travel by `canaryctl models upload` or `scp`. A
  capture profile saved in the Pi's dashboard is an override layered over the
  repo's catalog by name (revert with `POST
  /control/capture/profiles/{name}/revert`). Don't add a runtime writer to a
  tracked file; `tests/test_state_dir.py` makes every daemon path pick a side.
  `192.168.137.2` is the **Ethernet** link.
  In the vehicle the Pi often has no DNS, so `git pull` fails there: deploy
  with a bundle (`git bundle create x.bundle <pi-head>..main`, `scp`, then
  `git fetch x.bundle main:refs/remotes/bundle/x && git merge --ff-only`).
  Restart with `scripts/restart_canary.sh` (below).
- **Pull sessions as a tar stream, never per-file `rsync`.** A session is
  thousands of tiny parquet files; `rsync -a` spent ten minutes and moved
  nothing, where `ssh … 'tar -cf - <sessions>' | tar -xf -` moved 350 MB in
  six seconds.
- **No analysis on the Pi while it collects.** Pull the session and analyse
  it on the laptop. A `nice -19` pandas run once cost a 3-minute outage and
  150k dropped records. Daemon endpoints that read a whole session go through
  the bounded single-flight child and defer while a capture is recording.
- **Storage isolation**: directories with a leading `_` (e.g.
  `_archive/`, `_replay-*`) are excluded from training discovery
  (`src/preprocessing/loader.py`), the live-session listing (timestamp
  regex), and the retention sweep. Use this prefix for any data that
  shouldn't contaminate the training corpus.
- **Tag physical actions, not graph selections**: when binding signal
  observations to events, tag what the operator physically did to the
  vehicle (key on, brake applied, accelerated to N mph). NT510 graph
  selection alone doesn't narrow polling.
- **The dev vehicle (Xterra) is manual transmission**: no TCM on its
  bus; skip TCM steps in capture protocols for it. (A vehicle-specific
  fact, not a platform assumption — TCM-equipped vehicles are in
  scope.)
- **Generation-wide facts go in a platform, not a copy per profile.**
  `data/platforms/<id>/` (a `platform.yaml` naming make, model and model
  years, plus the shared files) serves every profile it covers, and a
  profile's own `data/<slug>/` file of the same name overrides it
  (`src.vehicle.platforms.vehicle_data_file`). The ROM backup recipe is shared
  this way by every 2005-2015 Xterra (2026-10-23, after a 2013 was refused a
  backup for lacking a copy). A new reader of generation-wide data uses the
  same lookup rather than requiring the file in each profile.
- **Fix at source, quarantine bad data**: telemetry is replaceable —
  fix collection and archive bad sessions rather than building runtime
  tolerance/threshold machinery. Prefer principled solutions over
  threshold hacks.

## Make it impossible, not documented

Three defects landed in a single day's work on the tuning toolkit, all the
same shape: something was *learned*, written into a docstring or commit
message, and left unenforced. Two were fixed by using code written hours
earlier in the same session. These are the habits that would have caught
them.

**A fix that every caller must remember is at the wrong layer.** The live
session resolver excluded `_`-prefixed capture directories, so every "what is
happening right now" read — `/status`, `/telemetry/latest`, OBD coverage, the
replay guard — silently showed the *previous* session for the whole duration
of a capture. The first fix threaded an optional argument through one call
site. There were six, and two were still broken a day later. Push the
correctness into the shared function, or change the signature so the unsafe
call cannot be written.

**Changing a contract means sweeping the callers.** `grep` for the function
before you decide a fix is complete. Both incomplete fixes above were one
`grep -rn` from being complete.

**A measured constraint belongs in a schema, not a comment.** The noise floor
was measured under WOT; across all conditions the same data gives a 16x larger
spread. That was recorded in a prose `note`, so nothing stopped the WOT figure
being quoted against an idle comparison — turning ordinary jitter into a
"finding". Making `conditions` a required field took minutes and made the
misuse unrepresentable. Ask: *did I learn a fact, or a constraint?* Constraints
become required fields, validators, or refusals that name the incident.

**Never order sessions by directory name.** Captures carry prefixes
(`_capture-fuel-map-…`, `_tuning-…`), so string order sorts by prefix first
and a capture a month later compares as "earlier". Use
`src.storage.sessions.session_started_at` / `latest_session` — one parser,
ordering by parsed time. This has now been re-derived four separate times.

**Two questions that look like one:** "which sessions may train?" excludes
captures by design (`preprocessing.loader.discover_sessions`); "which session
is live?" must include them (`storage.sessions.latest_session`). Conflating
them is what caused the bug above.

**Never compare two signals by their aggregates.** This is the single most
expensive mistake in the project's history — six retracted findings across
three months, all the same shape:

- `ECM_CALC_LOAD_B` was bound to a byte because its per-window *mean* matched
  another byte's mean. It is the low byte of a 16-bit rpm counter, which
  averages near mid-scale whatever the engine does, so it matched anything
  else near mid-scale. Removed 2026-08-06.
- `achieved_hz_capture` reported a rate as *count ÷ wall-clock*. A capture
  polling at a solid 14.93 Hz read as 11.25 Hz because the window opened late.
- The Mode 22 "session cycling" diagnosis read *cumulative* counters as if
  they were scoped to the capture. Nothing was cycling.
- The "K responds 1:1" fuel result averaged across rpm bands whose
  *composition* differed between the two sides.

Use `src.analysis.compare.compare_channels` — the only sanctioned comparison.
It takes specs and a session and does the pairing itself, so a pre-aggregated
frame cannot be handed to it. It refuses too-few-pairs and
barely-moving-signal, the two cases where an aggregate yields a confident
number and same-instant values yield nothing. `n_pairs` counts *pairs*, never
either input's length. Ranking by r² is not enough either: over one drive
nearly every engine signal correlates with rpm, so read `scale_verdict`, which
separates *unstable* from *unresolvable* rather than condemning slow signals.
**Then look at the per-sample ratio anyway — the verdict misleads in both
directions.** It is computed from slope drift, and a fitted offset can absorb
what it is looking for: on 2026-09-20 a RAM word passed as `[stable]` at
r²=0.9966 while its ratio to the reference drifted ±14 %, and the next day a
genuine mirror was reported `[drifting]` (slope 1.0094, offset −52) while its
ratio sat at 1.0000 with 0.16 % spread.

**Reproduce a known answer before trusting a novel one.** Every retraction
above looked convincing in isolation and none was checked against a case with
a known answer first. `tests/test_analysis_ground_truth.py` runs the real
chain over a committed slice of a real drive and asserts it still lands where
independent instruments already agree; expectations are data
(`tests/fixtures/ground_truth/expectations.yaml`), so recording a new fact is
editing YAML. The strongest check there is nearly free: a broadcast binding
and the Mode 01 PID for the same quantity must agree **1:1** once both are in
engineering units — that one assertion catches a wrong byte offset,
endianness, scale, offset, or unit without anyone stating the right answer.
Note its measured blind spot: slope is a ratio, so a +4% scale error shows as
only −2.9% slope error, which is why exact decode parameters are pinned
separately in `catalog_pins`.

**An empty result must say which kind of empty it is.** "The gates filtered
everything" is an answer about the drive; "the inputs were never captured" is
not an answer at all, and they demand opposite responses. `delivery_error`
reported `started: 0` for a 25-minute all-DID capture with 18k samples per
channel — the `tuning-did` profile disables Mode 01, so commanded AFR was
absent by construction. It now refuses and names the cause.

## Package layers

Imports between `src/` packages run one way, down the layers.
`tests/test_layering.py` is the source of truth: its `LAYERS` map gives each
package its layer, and it fails on any upward import or any same-layer pair
that imports both ways. Function-level and `TYPE_CHECKING` imports count.
`OWED` and `OWED_PAIRS` are empty (R09, closed 2026-10-03); `STANDING` holds
the one deliberate exception, the daemon serving the dashboard app. A new
package needs a layer there before anything imports it. When the test
refuses an import, move the code to the layer it belongs to rather than
deferring the import into a function to hide the cycle; that habit is what
R09 spent nine items undoing.

## RAM (memory) channels

`data/dids.yaml` has a `memory:` catalog beside `dids:` — service-0x23
(ReadMemoryByAddress) channels, stored as `mem:<NAME>`. They exist because
some quantities are not DIDs at all: the ECM's final commanded equivalence
ratio lives at a RAM address, which is why 138 answering DIDs never found it.

- **Addresses belong to the ECU's program, not its calibration.** The
  flashes made on this vehicle rewrite calibration tables; they do not move
  RAM, so a reflash does not invalidate a memory channel (operator,
  2026-09-21). The daemon used to withhold every channel whose
  `verified_tune_revision` differed from the running calibration, and that
  blinded a whole tuning drive the morning after a flash. It no longer
  does (`6ef51b4`). `verified_tune_revision` is now provenance only: which
  calibration the address was *checked* on. A different *program* (another
  ECU, or an OS/software-number change) is a different memory map. Re-verify
  before trusting the channels there.
- **Verify against known answers, not by stamp.** `python -m
  tools.verify_memory_channels` re-reads each address and checks it against
  its declared `known_answer` relation (`clamped_high_byte`, `equals_did`,
  `linear_did`, `reciprocal_did`). `--record-revision <rev>` restamps the
  provenance, window by window. Run it when a channel looks wrong, not as a
  ritual after every flash.
- **Windows are the only same-instant multi-channel read on this ECM.** It
  refuses batched Mode 22 and every dynamic-identifier service on CAN (0xAC,
  0x2A, 0x2C — all tested 2026-09-20), so two DIDs are always two requests
  ~1.5 ms apart. Channels naming the same `window` share one ≤63-byte read.
- **A sentinel is a state, not a number.** The target word is zero during fuel
  cut; it decodes to no measurement and is reported on `mem:<NAME>_SENTINEL`.
- **A fast quantity cannot be identified by exact equality.** Ignition timing
  (changes on ~5 % of steps) resolved at 99.9 %; air charge (changes on ~82 %
  even at idle) could not, because it moves faster than the gap between the
  two reads. Use the per-sample ratio, and a steadier operating point.
- Verify a relation can *fail* before trusting that it passed: apply it to
  the neighbouring bytes. A constant matching a constant is not evidence.

## Restarting the daemon on the Pi

Use `scripts/restart_canary.sh`, never a bare `systemctl restart canary`. It
refuses while a capture is active. Capture state is deliberately ephemeral, so
a restart silently drops the daemon back to `default` and the rest of the drive
is recorded without the capture — on 2026-09-21 one agent's deploy ended
another agent's tuning capture four minutes into a drive, and nothing reported
it. `--force` exists for when ending the capture is the intent; it names what
it killed.

## Roadmap docs & task truth

Planned work lives in `docs/roadmap/` as checklist docs, with detailed
implementation checklists in `docs/roadmap/tasks/` (`D03-open-work.md` for
collection and diagnostics, including items carried out of archived
roadmaps; `W00`–`W07` for the workbench regroup and the Engine Tuning pages). Finished plans move to `docs/roadmap/archive/`, whose
README also says which fully-ticked documents deliberately did *not* move.
**Task-level truth is each doc's own checkboxes** (`[x]` done, `[~]` partial/blocked
with a note). `docs/roadmap/OVERNIGHT-QUEUE.md` orders the work and
records operator-approved "pre-made decisions" — when a task says a
decision was made, implement that option; never re-open it. `file:line`
anchors in task text date from review snapshots and drift — locate code
by the cited symbol name, treating line numbers as hints.

## Concurrent sessions share this working tree

More than one agent session may be active in this repo at once (e.g. an
overnight run plus a daytime terminal). Consequences:

- Commit with **explicit pathspecs** (`git commit <paths>`) so another
  session's staged or unstaged work is never swept into your commit.
- Never run tree-wide destructive git commands (`git restore .`,
  `git checkout .`, `git clean`) — revert only files you yourself changed.
  `git stash` is allowed (operator, 2026-10-12).
- If a file you need is already dirty with work you didn't do, treat it
  as owned by the other session: coordinate, don't overwrite.

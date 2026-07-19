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

**This is not a safety-critical system.** It is read-only on the CAN bus
(no `bus.send`, no actuation, no influence on vehicle behavior). Wrong
inference, a stalled daemon, a missing anomaly event, a deleted session
directory — none of these can affect the vehicle, the operator, or
anyone else on the road. The truck drives the same whether canary is
running or not.

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
- `engine_off` — CAN fresh, OBD stale. Plugged in, key out.
- `no_data` — neither bus fresh. Pi probably disconnected.
- `replay` — runtime replay is active; the "OBD"/"CAN" you see is
  re-emitted from a stored session.
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
- **Branching**: work directly on `main`, no feature branches or PRs.
- **Lint/test gate**: whole-repo `ruff check . && ruff format --check .
  && mypy src && pytest` before commit; dashboard changes additionally
  `pnpm test` and `pnpm typecheck` in `src/dashboard/static` (re-run
  `TuningFeedback.test.ts` once on flake before treating it as a
  regression). A pre-commit hook runs the fast gate (ruff + mypy)
  automatically.
- **Test tiers** (pytest runs in parallel via `-n auto`, needs
  `pytest-xdist` from the `dev` extra): bare `pytest` is the full
  **commit gate** — everything except `integration`, incl. the `slow`
  ML training/bake-off tests. While *iterating* on a task, run the
  inner-loop tier `pytest -m "not integration and not slow"` (~35 s;
  drops the heavy torch-training tests) and reserve the full run for the
  commit gate. `-m integration` / `-m slow` run those tiers explicitly;
  add `-n0` to force serial output when debugging one test.
- **Pi deployment path**: `ssh canary@192.168.137.2` (or
  `canary.local`); install via `sudo bash scripts/install_pi.sh`; data
  lives under `/data/canary/`; control token at
  `/etc/canary/control.token`. Model binaries are gitignored — they
  travel by `scp`, not `git pull`.
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
- **Fix at source, quarantine bad data**: telemetry is replaceable —
  fix collection and archive bad sessions rather than building runtime
  tolerance/threshold machinery. Prefer principled solutions over
  threshold hacks.

## Roadmap docs & task truth

Planned work lives in `docs/roadmap/` as checklist docs; **task-level
truth is each doc's own checkboxes** (`[x]` done, `[~]` partial/blocked
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
  `git checkout .`, `git stash`, `git clean`) — revert only files you
  yourself changed.
- If a file you need is already dirty with work you didn't do, treat it
  as owned by the other session: coordinate, don't overwrite.

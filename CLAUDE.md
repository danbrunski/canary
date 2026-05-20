# CANary — agent context

Quick orientation for AI agents working in this repo. Project README and
`docs/CANary-overview.md` are the human-facing entry points; this file is
the *agent-facing* one.

## What this is (and isn't)

CANary is a **hobby / research project** — one operator, one 2008 Nissan
Xterra, mounted on a Raspberry Pi 5. It logs telemetry, learns "normal"
operation with unsupervised models, and surfaces alerts on a local
dashboard.

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
  push, the operator pulls on the Pi. See
  `~/.claude/projects/-home-dan-code-canary/memory/feedback_main_branch_only.md`.
- Don't over-engineer for failure modes the system can't actually have.
  The detectors firing 200×/sec at saturation severity is annoying, not
  dangerous — don't add runtime kill-switches; fix the model.

## What MCP gives you

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

The MCP session can die when `canary-mcp` is restarted (server
invalidates session ids, client doesn't auto-reconnect). When that
happens, fall back to direct HTTP against `http://canary.local:8000`
with `Authorization: Bearer $CANARY_CONTROL_TOKEN` — every MCP tool
has an HTTP counterpart under `/control/...` or `/anomalies` etc.
Restarting `claude` re-establishes MCP cleanly.

## Project conventions

- **Python**: always `.venv/bin/python` (3.12), never system `python3`.
- **Branching**: work directly on `main`, no feature branches or PRs.
- **Lint/test gate**: whole-repo `ruff check . && ruff format --check . && mypy src && pytest` before commit. A pre-commit hook runs the fast gate (ruff + mypy) automatically.
- **Pi deployment path**: `ssh canary@192.168.137.2` (or `canary.local`); install via `sudo bash scripts/install_pi.sh`; data lives under `/data/canary/`; control token at `/etc/canary/control.token`.
- **Storage isolation**: directories with a leading `_` (e.g. `_archive/`, `_replay-*`) are excluded from training discovery (`src/preprocessing/loader.py`), the live-session listing (timestamp regex), and the retention sweep. Use this prefix for any data that shouldn't contaminate the training corpus.
- **Tag physical actions, not graph selections**: when binding signal observations to events, tag what the operator physically did to the vehicle (key on, brake applied, accelerated to N mph). NT510 graph selection alone doesn't narrow polling.
- **Vehicle is manual transmission**: no TCM on the bus; skip TCM steps in capture protocols.

## Memory

The operator's preferences and project state are kept in
`~/.claude/projects/-home-dan-code-canary/memory/MEMORY.md`. Read it
when you join a conversation; update it when you learn something
durable. Don't write secrets there.

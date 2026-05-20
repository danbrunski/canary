# MCP server (`canary-mcp`)

The CANary MCP server exposes the running daemon to MCP-speaking clients —
Claude Code on a laptop, Qwen-Agent on the Pi, anything that speaks the
[Model Context Protocol](https://modelcontextprotocol.io/).

It's a separate process from `src.daemon`. It calls the dashboard HTTP
control plane on localhost for live state and reads Parquet directly for
ad-hoc SQL. Adding it doesn't touch the capture / inference hot path.

---

## What's exposed

Read tools:

| Tool | Wraps |
|---|---|
| `get_status` | `GET /status` (incl. derived `vehicle_state.{state, rpm, coolant_temp, run_time, obd_freshness_seconds, can_freshness_seconds, replay_id}` — check this before concluding anything about detector behavior; states are `engine_running`, `ignition_only`, `engine_off`, `no_data`, `replay`, `unknown`) |
| `get_config` | `GET /config` |
| `get_recent_anomalies` | `GET /anomalies` (+ client-side source/mode filter) |
| `get_recent_telemetry` | `GET /telemetry/latest` |
| `get_behavior_live` | `GET /models/behavior/live` |
| `get_behavior_models` | `GET /models/behavior` |
| `get_current_capture` | `GET /control/captures/current` |
| `get_bindings` | `GET /bindings` |
| `get_dids` | `GET /dids` |
| `get_obd_coverage` | `GET /obd/coverage` |
| `list_sessions` / `get_session` | `GET /control/sessions[/{id}]` |
| `get_dataset_health` | `GET /control/datasets/health` |
| `get_training_runs` | `GET /control/training-runs` |
| `list_jobs` / `get_job` / `get_job_logs` | `GET /control/jobs[...]` |
| `get_tuning` / `get_feedback` | `GET /control/{tuning,feedback}` |
| `tail_daemon_log` | `journalctl --unit canary -o json` (fallback file when no systemd) |
| `search_anomaly_log` | structured search over anomaly day-files + MCP audit log (kind=anomalies\|audit) |
| `signal_lookup` | in-process FSM-name fuzzy match (requires `CANARY_SIGNALS_PATH`) |
| `query_telemetry` | DuckDB over the Parquet ring buffer (requires `CANARY_STORAGE_PATH`) |

Write tools (audit-logged with `actor="mcp"` via
[`src/control/audit.py`](../src/control/audit.py)):

| Tool | Wraps |
|---|---|
| `start_capture` / `tag_current_capture` / `stop_capture` | `POST /control/captures[...]` |
| `record_feedback` | `POST /control/feedback` |
| `set_session_metadata` / `pin_session` / `unpin_session` | `POST /control/sessions/{id}[...]` |
| `start_job` / `cancel_job` | `POST /control/jobs[...]` |
| `set_tuning` / `estimate_tuning` / `revert_tuning` | `POST /control/tuning[...]` |
| `upload_model_artifact` | base64-encodes a local artifact + `POST /control/models/{type}/artifact` |
| `replay_window` | submits a `replay_window` daemon job, polls until done, returns the inference report |
| `start_replay` / `stop_replay` / `get_replay_status` | `POST /control/replay[...]` — runtime replay-into-collector with isolated outputs (see "End-to-end session replay" below) |

Resources (URI-addressable; clients pin these to a conversation for stable context):

| URI | Body |
|---|---|
| `canary://config` | daemon canary.yaml as JSON |
| `canary://status` | daemon status snapshot |
| `canary://behavior-models` | learned behavior surfaces |
| `canary://sessions/{session_id}` | one session's metadata + chunk inventory |
| `canary://anomalies/{date}` | raw JSONL for `YYYY-MM-DD` (gated on `CANARY_ANOMALIES_DIR` / `alerts.anomalies_dir`) |
| `canary://audit/mcp` | tail of the MCP audit log |

---

## Transports

`canary-mcp` ships with three transports:

- **`streamable-http`** (default) — for LAN clients. Binds to
  `CANARY_MCP_HOST:CANARY_MCP_PORT` (default `0.0.0.0:8001`) and serves at
  `/mcp`. When `CANARY_CONTROL_TOKEN` is set (the install path always sets
  it), every request must carry `Authorization: Bearer <token>` — others
  get `401 Unauthorized` from FastMCP's `RequireAuthMiddleware`. Leaving
  the token unset keeps the transport open, intended only for local dev
  tunnelling.
- **`stdio`** — for local debugging or SSH-tunneled access. No port, no
  auth (whoever spawns the process is already trusted).
- **`sse`** — legacy SSE transport for older clients.

Invoke:

```bash
canary-mcp                       # streamable-http on $CANARY_MCP_HOST:$CANARY_MCP_PORT
canary-mcp --stdio               # local stdio (or via ssh)
canary-mcp --transport sse       # legacy SSE
```

---

## Setup on the Pi

The daemon's install path (`scripts/install_deps.py`) pulls in `mcp`,
`duckdb`, and `httpx` automatically — they're base dependencies in
[`pyproject.toml`](../pyproject.toml).

Enable the unit:

```bash
sudo systemctl enable --now canary-mcp.service
```

Environment variables (set in `/etc/canary/canary.env` alongside the daemon's):

```ini
CANARY_DAEMON_URL=http://127.0.0.1:8000
CANARY_CONTROL_TOKEN=<shared with the daemon>     # required for HTTP/SSE inbound auth
CANARY_DATA_DIR=/data/canary                       # matches scripts/install_pi.sh
CANARY_STORAGE_PATH=/data/canary/sessions          # falls back to $CANARY_CONFIG
CANARY_MCP_HOST=0.0.0.0
CANARY_MCP_PORT=8001
CANARY_MCP_AUDIT_PATH=/data/canary/audit/mcp.jsonl # defaults to $CANARY_DATA_DIR/audit/mcp.jsonl
CANARY_SIGNALS_PATH=/opt/canary/data/fsm/signals.json   # enables signal_lookup
CANARY_MCP_LOG_FALLBACK=/var/log/canary/daemon.jsonl    # used if journalctl absent
CANARY_MCP_DAEMON_UNIT=canary                            # systemd unit for tail
CANARY_ANOMALIES_DIR=/data/canary/anomalies              # falls back to canary.yaml
```

---

## Connecting Claude Code

The repo ships a [`.mcp.json`](../.mcp.json) pointing at
`http://canary.local:8001/mcp` with `Authorization: Bearer ${CANARY_CONTROL_TOKEN}`.
Clone the repo locally, export the token, and Claude Code picks it up:

```bash
export CANARY_CONTROL_TOKEN=<value from /etc/canary/control.token>
claude   # cd into the repo first
```

For stdio-over-SSH (no LAN exposure), replace `.mcp.json` with:

```json
{
  "mcpServers": {
    "canary": {
      "type": "stdio",
      "command": "ssh",
      "args": ["canary.local", "canary-mcp", "--stdio"]
    }
  }
}
```

---

## Connecting Qwen on the Pi

Any MCP-aware Qwen runtime (Qwen-Agent, an Ollama client wrapping
tool-call mode, etc.) can point at `http://127.0.0.1:8001/mcp` with the
same bearer token. Same tool surface, two clients.

See also [`docs/llm-assist-and-compute-roadmap.md`](./llm-assist-and-compute-roadmap.md)
for the broader on-device LLM plan; the MCP server is what gives every
listed feature its data hook.

---

## Inference debugging via `replay_window`

The MCP tool submits a `replay_window` daemon job that re-runs the current
Isolation Forest + LSTM autoencoder over a session window, looks up the
anomaly events that historically fired during the same window, and returns
a structured comparison:

```json
{
  "session_id": "2026-05-17T120000",
  "window": {"start": "...", "end": "...", "actual_start": "...", "actual_end": "..."},
  "summary": {
    "rows_scored": 1800,
    "if_anomalies_replayed": 2,
    "lstm_windows_scored": 1741,
    "lstm_anomalies_replayed": 1,
    "historical_events": 3,
    "matched": 2,
    "replayed_only": 1,
    "historical_only": 1
  },
  "isolation_forest": {"loaded": true, "scores": [...], "anomalies": [...]},
  "lstm_autoencoder": {
    "loaded": true,
    "windows": [
      {"timestamp": "...", "global_score": 0.42, "channel_scores": {...}, ...}
    ],
    "anomalies": [...]
  },
  "historical_events": [...],
  "diff": {"matched": 2, "replayed_only": 1, "historical_only": 1, "tolerance_seconds": 2.0},
  "warnings": []
}
```

The heavy lifting (torch, scikit-learn, the preprocessing pipeline) runs in
the daemon's job runner — the MCP server itself stays lightweight. Missing
artifacts are reported as `loaded=false` rather than failing the job, so the
report shows "as much as is recoverable" when only one model is deployed.

Use cases:
- **Why did this fire?** Replay the window, inspect the per-channel LSTM
  reconstruction errors at the offending timestamp.
- **Did my model change fix it?** Replace the deployed artifact, replay the
  same known-anomalous window, diff against the old historical event.
- **Why didn't it fire?** Replay a window where the operator expected an
  anomaly; the IF/LSTM score traces show how close it came.

---

## End-to-end session replay (`start_replay` / `stop_replay`)

`replay_window` is an offline batch — it re-scores one stored window in
the daemon's job runner and hands back a report. When you want to
exercise the *whole* pipeline (drain → Parquet → live inference →
emitter → anomaly broadcaster) as if the truck were running, use the
runtime replay instead:

```text
mcp__canary__start_replay(session_id="2026-05-18T033527", speed=3.0)
mcp__canary__get_replay_status()
mcp__canary__stop_replay()
```

The daemon re-emits the stored session's telemetry onto the same
asyncio queue real CAN/OBD collectors feed, so IF + LSTM + behavior
detectors fire on the replayed stream and the dashboard's WebSocket
feeds light up live.

**Isolation contract** — replays never contaminate real data:

* Parquet writes go to `<storage>/_replay-<replay_id>/`. The leading
  `_` excludes the dir from
  [`discover_sessions`](../src/preprocessing/loader.py) (training input),
  from the `GET /control/sessions` regex filter, and from the
  timestamp-based retention sweep.
* Anomalies go to `<anomalies_dir>/_replay/<replay_id>.jsonl`. The
  `_replay/` subdir is outside
  [`read_events_dir`](../src/alerts/event_logger.py)'s glob, so feedback
  / refit jobs never see replay anomalies as real.
* A separate retention sweeper evicts `_replay-*` dirs after
  `collection.replay_retention_days` (default 7) so the disk doesn't
  silently fill.

**Caveat:** real CAN/OBD collectors keep running during a replay (the
daemon doesn't stop them). On a Pi sitting on the bench this is fine;
if you ever start a replay while live data is genuinely flowing, the
two streams interleave into `_replay-<id>/`. Stop the live capture
first if you care about clean replay output.

---

## SQL via `query_telemetry`

The tool exposes a DuckDB view named `telemetry` over every
`telemetry-*.parquet` file under `CANARY_STORAGE_PATH`. Columns match
[`src/storage/parquet_writer.py`](../src/storage/parquet_writer.py) plus a
derived `session` column from the parent directory name.

Only single `SELECT`/`WITH` statements are accepted; `ATTACH`, `INSERT`,
`PRAGMA`, etc. are rejected before DuckDB sees them. An outer `LIMIT 5000`
is wrapped around every query unless the caller passes `row_limit`.

Example queries Claude might issue:

```sql
-- "Show me the LTFT timeline on highway from the last drive"
SELECT timestamp, value_number
FROM telemetry
WHERE channel = 'LTFT_B1'
  AND session = (SELECT max(session) FROM telemetry)
ORDER BY timestamp;

-- "Compare RPM distribution per operating mode across recent sessions"
SELECT session, channel, count(*) AS samples,
       min(value_number), avg(value_number), max(value_number)
FROM telemetry
WHERE channel = 'RPM'
GROUP BY session, channel
ORDER BY session DESC;
```

---

## Auditing

Every write-tool invocation appends a record to
`CANARY_MCP_AUDIT_PATH` (default `/var/lib/canary/audit/mcp.jsonl`),
namespaced `mcp.<tool>` and tagged `actor="mcp"`. Entries are written on
both success and failure so a later "what did Claude touch?" review can
diff against operator-driven changes (which still go through
[`ControlAuditLogger`](../src/control/audit.py) with
`actor` unset).

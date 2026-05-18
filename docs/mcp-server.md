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
| `get_status` | `GET /status` |
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
  `/mcp`. Honors `Authorization: Bearer ${CANARY_CONTROL_TOKEN}` when the
  token is set.
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
CANARY_CONTROL_TOKEN=<shared with the daemon>
CANARY_STORAGE_PATH=/var/lib/canary/sessions   # falls back to $CANARY_CONFIG
CANARY_MCP_HOST=0.0.0.0
CANARY_MCP_PORT=8001
CANARY_MCP_AUDIT_PATH=/var/lib/canary/audit/mcp.jsonl
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

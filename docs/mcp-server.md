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
| `get_status` | `GET /status` (incl. derived `vehicle_state.{state, rpm, coolant_temp, run_time, obd_freshness_seconds, can_freshness_seconds, replay_id}` — check this before concluding anything about detector behavior; states are `engine_running`, `ignition_only`, `engine_off`, `no_data`, `replay`, `unknown`. Also `model_provenance` — per-loaded-model `{model_version, created_at, training_data_hash, training_sessions, artifact_format_version}` off each manifest (Task 6.5); `health.can0` — can0's rx overrun/drop/error sysfs counters plus the delta since the daemon started watching, `null`/absent when there's no `can0` interface (Task 6.8, see `scripts/verify_artifacts.py` for the companion deploy-skew check, Task 6.6)) |
| `get_config` | `GET /config` |
| `get_power_status` | `GET /control/power` — UPS SoC/cell voltage, watchdog armed/on-battery/seconds-lost/grace, active keep-alive hold. Use instead of SSH'ing in to check the `ups-shutdown` watchdog (see [ups_shutdown_watch.py](../scripts/ups_shutdown_watch.py)); `watchdog_running: false` means the systemd unit isn't up, `watchdog_stale: true` means it's up but hung. |
| `get_recent_anomalies` | `GET /anomalies` (+ client-side source/mode filter) |
| `get_recent_telemetry` | `GET /telemetry/latest` |
| `get_behavior_live` | `GET /models/behavior/live` |
| `get_behavior_models` | `GET /models/behavior` |
| `get_current_capture` | `GET /control/captures/current` |
| `get_bindings` | `GET /bindings` |
| `get_dids` | `GET /dids` |
| `get_obd_coverage` | `GET /obd/coverage` |
| `get_dtc` | `GET /obd/dtc` — cached stored/pending/permanent DTC report (Mode 03/07/0A poll), MIL state, `captured_at`, `stale`. Read-only, no clear-codes tool. |
| `get_monitors` | `GET /obd/monitors` — cached Mode 06 on-board monitor sweep (Task 3.1): per-cylinder misfire counts (`misfire_counts`, cylinder number → raw Mode 06 test value), the full raw `results` list (monitor/test id, value/min/max, `passed`), `captured_at`, `stale`. Read-only. |
| `get_trends` | `GET /control/trends` — per-stat session-fingerprint drift: latest value, reference band, Mann-Kendall trend verdict, CUSUM alarm state, evaluated over the full fingerprint history against the frozen reference epoch. `available: false` when nothing's been frozen yet (`scripts/fingerprint.py freeze`). |
| `get_binned_analysis` | `GET /analysis/binned` — ad-hoc binned aggregation over a value/x[/y] channel triple ("trims by rpm×load") without hand-written SQL or a refit; `agg` is `mean`\|`median`\|`p95`\|`count`, scoped by `session`/`since`/`until` like `query_telemetry`. `available: false` with a `reason` when the scope has no data for the requested channels together. |
| `get_trip_summary` | `GET /sessions/{id}/trip-summary` — trip-summary sidecar for one session (duration, distance, fuel used, avg economy, CO2, idle time/fraction, max/avg speed, 0-60 mph, 1/4-mile ET/trap if a clean launch was captured). Computed on demand from the stored session and cached to `trip_summary.json` after the first request; also available as a CLI (`scripts/trip_report.py <session>`). |
| `get_session_digest` | `GET /control/sessions/{id}/digest` — end-of-drive digest for one session: duration, mode seconds, gated anomaly counts by source, top behavior-surface deviations, fingerprint deltas vs. the frozen reference epoch (top movers), new DTCs, new vehicle-ledger events. Authenticated — it aggregates fields (vehicle ledger, fingerprint z-scores) otherwise only readable via `/control/vehicle-ledger` and `/control/trends`. Written by the trip-end hook (sustained GPS moving->stationary, or daemon shutdown as a fallback trigger — see `GpsTracker`/`src.control.trip_end`); computed on demand and cached to `digest.json` if that hook hasn't fired yet for this session. Pushed via ntfy when `notifications.digest_enabled` is set (ships disabled by default). |
| `list_sessions` / `get_session` | `GET /control/sessions[/{id}]`; listing accepts ISO `since`/`until` bounds and a newest-match `limit`, so agents can locate "today's drive" without reading the whole catalog. |
| `get_session_timeseries` | `GET /control/sessions/{id}/chart` — aligned selected `source:CHANNEL` arrays from a stored Pi session without SSH or a Parquet export; defaults to 1 Hz for bounded whole-drive responses. |
| `get_session_classification` | `GET /control/sessions/{id}/classification` — mode/event/disposition sidecar for one session |
| `get_corpus_mode_summary` | `GET /control/classifications/summary` — per-mode seconds + event counts across the corpus |
| `list_events_by_kind` | `GET /control/classifications/events` — sessions containing a given derived event kind (e.g. `cold_start`) |
| `get_dataset_health` | `GET /control/datasets/health` |
| `get_training_runs` | `GET /control/training-runs` |
| `list_vehicle_events` | `GET /control/vehicle-ledger` — vehicle maintenance/intervention ledger (maintenance/mod/repair/observation/DTC), oldest-first, latest per id |
| `list_jobs` / `get_job` / `get_job_logs` | `GET /control/jobs[...]` |
| `get_tuning` / `get_feedback` | `GET /control/{tuning,feedback}` |
| `tail_daemon_log` | `journalctl --unit canary -o json` (fallback file when no systemd) |
| `get_service_health` | `systemctl show {canary,canary-mcp,ups-shutdown}` — is-active/sub-state/restart-count/uptime, no SSH needed. `restart_count` climbing across polls is the crash-loop tell (this is what would have caught the 2026-07-18 UPS watchdog crash-loop instantly). |
| `search_anomaly_log` | structured search over anomaly day-files + MCP audit log (kind=anomalies\|audit) |
| `signal_lookup` | in-process FSM-name fuzzy match (requires `CANARY_SIGNALS_PATH`) |
| `query_telemetry` | DuckDB over the Parquet ring buffer (requires `CANARY_STORAGE_PATH`) |
| `get_behavior_deviations` | Local, no daemon call (requires `CANARY_CONFIG`): diffs a *stored* session against the learned behavior surfaces via [`src/analysis/session_deviations.py`](../src/analysis/session_deviations.py) — the "did my change move behavior?" tool. Unlike `get_behavior_live` (current tick only), this replays a whole recorded drive through the training pipeline and reports per-surface n_observed/n_in_domain, mean actual/expected/deviation, warning/critical fraction, and (surface_2d) a per-(rpm,load)-bin breakdown. `mode=` filters to surfaces gated on that `operating_mode`. `against=<session_id>` (Task 5.4) A/Bs `session` directly against a second session instead — per-surface and per-binned-cell *deltas* (`mean_deviation_delta`, etc., `b - a`) for a before-mod/after-mod comparison. Also available as a CLI: `scripts/analyze_session.py --session <id> [--mode <m>] [--against <id2>] [--profile <slug>]`. |

Write tools (audit-logged with `actor="mcp"` via
[`src/control/audit.py`](../src/control/audit.py)):

| Tool | Wraps |
|---|---|
| `start_capture` / `tag_current_capture` / `stop_capture` | `POST /control/captures[...]` |
| `record_feedback` | `POST /control/feedback` |
| `record_vehicle_event` | `POST /control/vehicle-ledger` — append a maintenance/mod/repair/observation entry (auto-appended for a new stored DTC too, see `src/control/dtc_state.py`); `corpus_boundary=True` flags an event that changes what "normal" looks like, for `--since-boundary` on `scripts/retrain.py` / `scripts/refit_behavior_models.py` |
| `set_session_metadata` / `pin_session` / `unpin_session` | `POST /control/sessions/{id}[...]` |
| `classify_session` | `POST /control/sessions/{id}/classification` — (re)run the session classifier and persist the sidecar |
| `start_job` / `cancel_job` | `POST /control/jobs[...]` |
| `set_tuning` / `estimate_tuning` / `revert_tuning` | `POST /control/tuning[...]` |
| `set_ups_keepalive` | `POST /control/power/keepalive` — set (minutes>0) or cancel (minutes=0) the UPS watchdog keep-alive hold, for a bench transition longer than the watchdog's grace window |
| `set_tuning_mode` | `POST /control/tuning-mode` — arm/disarm the focused tuning poll profile. Restricts Mode 01 to a small tuning set and suspends DID windows so each channel gets far more of the bus budget. Channels outside the set stop updating while armed — expected, not a fault |
| `set_passive_mode` | `POST /control/passive` — arm/disarm receive-only mode. Daemon keeps logging CAN and scoring anomalies but transmits nothing. Arm whenever another tool owns a diagnostic session (ECU reflash, actuator test). OBD channels go stale while armed — expected, not a fault; `get_status` reports `passive.enabled` |
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

### Session resilience: surviving a `canary-mcp` restart

By default, `streamable-http` is *stateful* — each client connection gets a
session id tracked in the server process's memory. Restart `canary-mcp`
(redeploy, crash, a systemd flap) and that in-memory table is wiped: any
client still holding the old session id gets rejected, and today the fix is
"restart claude" or fall back to raw HTTP against `/control/...` (see
`CLAUDE.md`). This is root-caused: FastMCP's `StreamableHTTPSessionManager`
only tracks sessions when `stateless=False` (the default) — it's a server-side
in-memory map, not anything the client can recover from on its own.

Set `CANARY_MCP_STATELESS=1` to pass `stateless_http=True` to FastMCP: no
session id is issued at all, so a restart can never strand a client. The
trade-off is losing the SSE resumable-stream / server-push machinery that
depends on a persistent session — this codebase's tools don't currently rely
on that (no `@mcp.resource` subscriptions, no server-initiated push), so it's
likely safe, but it's opt-in rather than the new default until it's been run
that way for a while. Add it to `/etc/canary/canary.env` alongside the other
`CANARY_MCP_*` vars and restart `canary-mcp` to pick it up.

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
CANARY_MCP_STATELESS=1                # opt-in: survive canary-mcp restarts, see below
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

See also [`docs/roadmap/llm-assist-and-compute-roadmap.md`](./roadmap/llm-assist-and-compute-roadmap.md)
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

Optional `session=`, `since=`, `until=` scope which sessions' parquet build
the `telemetry` view for that call, *before* DuckDB touches them — cheaper
than a `WHERE session = '...'` filter (which still works, but scans every
session's parquet content first). `session` matches a session id exactly;
`since`/`until` accept ISO-8601 (date or datetime) or a bare session id and
bound each session's own timestamp inclusively.

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
`CANARY_MCP_AUDIT_PATH` (default `/data/canary/audit/mcp.jsonl`, i.e.
`$CANARY_DATA_DIR/audit/mcp.jsonl`),
namespaced `mcp.<tool>` and tagged `actor="mcp"`. Entries are written on
both success and failure so a later "what did Claude touch?" review can
diff against operator-driven changes (which still go through
[`ControlAuditLogger`](../src/control/audit.py) with
`actor` unset).

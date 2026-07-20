# Headless API

The Raspberry Pi runs one CANary daemon. That daemon collects telemetry,
writes Parquet, runs model polling/inference, serves the dashboard, and owns
the authenticated control API used from a laptop.

The read-only dashboard endpoints are unauthenticated in v1. The `/control/*`
endpoints require a bearer token from `CANARY_CONTROL_TOKEN`.

This API is only reachable while the Pi is powered. The offline workflow for
syncing sessions to a laptop/tablet, reviewing data while the truck is off,
choosing whether training runs locally or on the Pi, and publishing models back
to the Pi is covered in `docs/companion-app-plan.md`. That workflow should use
the same CANary app shell as the Pi dashboard with a local-library backend.

A separate process — `canary-mcp` — exposes this same surface to LLM clients
(Claude Code, Qwen, anything that speaks the Model Context Protocol). It
runs as its own systemd unit and adds an ad-hoc-SQL tool over the Parquet
ring buffer on top of the HTTP endpoints. See
[`docs/mcp-server.md`](./mcp-server.md).

## Browser

If the dashboard frontend has been built, open:

```bash
http://canary.local:8000/
```

If the frontend has not been built yet, the API still works.

## REST

```bash
curl http://canary.local:8000/status
curl http://canary.local:8000/config
curl 'http://canary.local:8000/telemetry/latest?n=50'
curl 'http://canary.local:8000/anomalies?since=2026-05-16T00:00:00Z'
curl http://canary.local:8000/bindings
curl http://canary.local:8000/models/behavior
```

Endpoint summary:

| Endpoint | Purpose |
| --- | --- |
| `GET /status` | storage path/free space, latest session, loaded model versions, vehicle, anomaly counters |
| `GET /config` | JSON view of the loaded `canary.yaml` |
| `GET /telemetry/latest?n=N` | newest `N` telemetry rows from disk |
| `GET /anomalies?since=...` | persisted anomaly events from JSONL |
| `GET /bindings` | decoded CAN binding metadata for display |
| `GET /dids` | Mode 22 DID catalog metadata |
| `GET /models/behavior` | behavior-model surfaces for dashboard panels |
| `GET /models/behavior/live?series=` | per-model expected/actual/deviation live view (`series=false` drops the rolling trail) |
| `GET /obd/coverage` | OBD PID/DID coverage summary |
| `GET /can/coverage` | broadcast CAN ID coverage summary |
| `GET /test-profiles` | available UI test profiles |
| `GET /obd/dtc` | cached stored/pending/permanent DTC report, MIL state, staleness |
| `GET /sessions/{id}/trip-summary` | trip-summary sidecar (duration, distance, fuel/economy/CO2, idle time/fraction, max/avg speed, 0-60, 1/4-mile) — computed on demand from the stored session on first request, cached to `trip_summary.json` after |

## Control API

The Pi installer writes a token to `/etc/canary/control.token` and exports it
to the systemd service as `CANARY_CONTROL_TOKEN`. On the laptop:

```bash
export CANARY_HOST=http://192.168.0.230:8000
export CANARY_CONTROL_TOKEN='<token from /etc/canary/control.token>'
```

Or persist them once so every shell picks them up — `canaryctl` reads
`~/.canary/env` (dotenv style) when the variables aren't already exported:

```bash
mkdir -p ~/.canary && chmod 700 ~/.canary
cat > ~/.canary/env <<'EOF'
CANARY_HOST=http://192.168.0.230:8000
CANARY_CONTROL_TOKEN=<token from /etc/canary/control.token>
EOF
chmod 600 ~/.canary/env
```

Precedence: `--host/--token` flags > exported env vars > `~/.canary/env`.
Point elsewhere with `CANARY_ENV_FILE`. The older `CANARY_TOKEN` name is
still accepted as a fallback for existing env files.

Then use `canaryctl`:

```bash
scripts/canaryctl.py status
scripts/canaryctl.py preflight
scripts/canaryctl.py version
scripts/canaryctl.py capture start --purpose "next drive" --protocol drive
scripts/canaryctl.py tag cold_start
scripts/canaryctl.py tag highway_cruise --field speed_target=70
scripts/canaryctl.py capture current
scripts/canaryctl.py capture stop --reason arrived
scripts/canaryctl.py sessions list
scripts/canaryctl.py sessions metadata 2026-05-17T120000 --field route=mesa
scripts/canaryctl.py sessions pin 2026-05-17T120000 --reason "training candidate"
scripts/canaryctl.py sessions export 2026-05-17T120000 --out drive.zip
scripts/canaryctl.py jobs start corpus_report
scripts/canaryctl.py jobs start retrain_if --days 14
scripts/canaryctl.py jobs show <job_id>
scripts/canaryctl.py jobs logs <job_id>
scripts/canaryctl.py jobs cancel <job_id>
scripts/canaryctl.py models upload isolation_forest \
  --artifact models/isolation_forest/model.joblib \
  --manifest models/isolation_forest/model.manifest.json
```

Equivalent raw HTTP:

```bash
curl -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"purpose":"next drive","protocol":"drive"}' \
  "$CANARY_HOST/control/captures"

curl -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"label":"highway_cruise","fields":{"speed_target":70}}' \
  "$CANARY_HOST/control/captures/current/tags"

curl -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"arrived"}' \
  "$CANARY_HOST/control/captures/current/stop"
```

Capture commands create a labeled window on the daemon's current telemetry
session. They do not start a second recorder process or contend for the CAN
adapter. Start/tag/stop records are appended to the active session's
`tags.jsonl`, and capture/session metadata is written to `control.json` inside
the session directory.

Control endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /control/preflight` | run storage/session/token/CAN/model/telemetry readiness checks |
| `GET /control/version` | return control API version, package version, git SHA, and token env name |
| `POST /control/captures` | start a labeled capture window on the active daemon session |
| `GET /control/captures/current` | inspect the active capture window |
| `POST /control/captures/current/tags` | append a tag to the active capture's `tags.jsonl` |
| `POST /control/captures/current/stop` | stop the active capture window |
| `GET /control/sessions` | list timestamped session directories |
| `GET /control/sessions/{id}` | inspect one session |
| `POST /control/sessions/{id}/metadata` | merge or replace operator metadata in `control.json` |
| `POST /control/sessions/{id}/pin` | pin a session so retention cleanup skips it |
| `POST /control/sessions/{id}/unpin` | remove a session's retention pin |
| `DELETE /control/sessions/{id}?force=true` | delete a session; `force=true` is required for pinned sessions |
| `GET /control/sessions/{id}/export` | download one session as a zip archive |
| `POST /control/jobs` | start an allow-listed background job |
| `GET /control/jobs` | list background jobs from the persisted job ledger |
| `GET /control/jobs/{id}` | inspect one job |
| `GET /control/jobs/{id}/logs` | fetch captured stdout/stderr for one job |
| `POST /control/jobs/{id}/cancel` | request cancellation for a queued or running job |
| `POST /control/models/{type}/artifact` | publish an uploaded model artifact and manifest |
| `GET /control/sessions/{id}/classification` | read a session's mode/event/disposition sidecar |
| `POST /control/sessions/{id}/classification` | (re)run the session classifier and persist the sidecar |
| `GET /control/classifications/summary` | corpus-wide per-mode seconds + event counts |
| `GET /control/classifications/events` | sessions containing a given derived event kind |
| `GET /control/sequence-detector` / `POST /control/sequence-detector` | inspect / switch the live sequence backbone (lstm\|tcn) |
| `POST /control/replay` / `POST /control/replay/stop` / `GET /control/replay` | run a stored session through the live pipeline with isolated `_replay-*` outputs |
| `GET /control/profiles` / `POST /control/profiles/{slug}/name` | list vehicle profiles / set a profile display name |
| `GET /control/power` / `POST /control/power/keepalive` | UPS watchdog status / set or cancel the keep-alive hold |
| `GET /control/feedback` / `POST /control/feedback` | read / record anomaly-feedback verdicts |
| `GET /control/training-runs` | training-run ledger |
| `GET /control/datasets/health` | corpus readiness + per-mode counts |
| `GET /control/tuning` / `POST /control/tuning` / `POST /control/tuning/estimate` / `POST /control/tuning/revert` | inspect / set / estimate / revert alert-tuning overrides |

Allowed job kinds:

| Kind | What It Runs |
| --- | --- |
| `retrain_if` | `scripts/retrain.py --skip-sequence` |
| `retrain_lstm` | `scripts/retrain.py --skip-if --sequence-model lstm` |
| `retrain_tcn` | `scripts/retrain.py --skip-if --sequence-model tcn` |
| `retrain_all` | `scripts/retrain.py` (IF + the configured sequence backbone) |
| `refit_behavior_models` | `scripts/refit_behavior_models.py` |
| `corpus_report` | `tools/corpus_report.py --out data/analyses/...` |

Job parameters are structured JSON, not shell text. Common parameters include
`days`, `all`, and `session_ids`. LSTM jobs also accept `device`; behavior
refits accept `recipes` and `dry_run`.

Job state is append-only in `daemon.jobs_dir/jobs.jsonl`, so recent job history
survives daemon restarts. Control mutations are appended to
`daemon.audit_log` as JSONL audit events and are also broadcast live.

## WebSockets

Live telemetry and anomalies stream over WebSockets:

```bash
websocat ws://canary.local:8000/ws/telemetry
websocat ws://canary.local:8000/ws/anomalies
websocat ws://canary.local:8000/ws/scores
websocat "ws://canary.local:8000/ws/control?token=$CANARY_CONTROL_TOKEN"
```

The dashboard uses the same streams. Slow clients drop old messages rather
than blocking collection.

## Laptop Development

For backend-only work, SSH into the Pi and use systemd:

```bash
ssh <pi-user>@canary.local
sudo journalctl -u canary -f
sudo systemctl restart canary
```

For local frontend development from a laptop, forward the Pi backend to local
port 8000 and run Vite locally:

```bash
ssh -L 8000:127.0.0.1:8000 <pi-user>@canary.local
cd src/dashboard/static
pnpm dev
```

Vite proxies `/status`, `/config`, `/telemetry`, `/anomalies`, and `/ws/*` to
`127.0.0.1:8000`, so the laptop browser talks to the Pi through the SSH tunnel.

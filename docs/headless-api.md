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
| `GET /obd/monitors` | cached Mode 06 on-board monitor sweep — per-cylinder misfire counts, raw monitor test results (value/min/max, pass/fail), staleness |
| `GET /sessions/{id}/trip-summary` | trip-summary sidecar (duration, distance, fuel/economy/CO2, idle time/fraction, max/avg speed, 0-60, 1/4-mile) — computed on demand from the stored session on first request, cached to `trip_summary.json` after |
| `GET /analysis/binned?value=&x=&y=&x_bins=&y_bins=&agg=&session=&since=&until=&units=` | ad-hoc binned aggregation over the DuckDB parquet layer ("trims by rpm×load") — `agg` is `mean`\|`median`\|`p95`\|`count`; `y`/`y_bins` optional (1D when omitted); `units` is `stored` (default) or `engineering`, the latter rescaling `decoded:` channels via `bindings.yaml` (they are stored raw on purpose — `decoded:ECM_RPM` is 8 counts/rpm) |

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
| `GET /control/sessions?since=&until=&limit=` | list timestamped session directories; optional ISO-8601 start-time bounds and newest-match limit |
| `GET /control/sessions/{id}` | inspect one session |
| `POST /control/sessions/{id}/metadata` | merge or replace operator metadata in `control.json` |
| `POST /control/sessions/{id}/pin` | pin a session so retention cleanup skips it |
| `POST /control/sessions/{id}/unpin` | remove a session's retention pin |
| `DELETE /control/sessions/{id}?force=true` | delete a session; `force=true` is required for pinned sessions |
| `GET /control/sessions/{id}/export` | download one session as a zip archive |
| `GET /control/sessions/{id}/chart?channels=&resample_hz=` | per-channel time-series arrays for a stored session (comma-separated `source:channel` keys); omit `resample_hz` to keep each channel's own native timestamps, set it to align every channel onto one shared forward-filled grid |
| `GET /control/sessions/{id}/gps.gpx` / `GET /control/sessions/{id}/gps.kml` | download the session's captured GPS track as GPX or KML |
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
| `GET /control/passive` / `POST /control/passive` | inspect / arm the CAN transmit gate (receive-only mode) — see [Passive mode](#passive-mode) |
| `POST /control/trigger_poll` | force specific channels due on the OBD scheduler's next tick — `{channels}` — instead of waiting out their tier cadence |
| `POST /control/can_activity` | capture N seconds (capped at 30) of raw CAN traffic — `{seconds, ids?}` — per-id frame rate + which byte positions changed |
| `GET /control/capture/profiles` | list strict, vehicle-scoped capture profiles |
| `PUT /control/capture/profiles` / `DELETE /control/capture/profiles/{name}` | save or delete a capture profile (`default` cannot be deleted) |
| `POST /control/capture` / `DELETE /control/capture` | start a named isolated capture / return to default collection |

`GET /control/sessions?captures=true` lists only `_capture-*` sessions and
tags each from its `capture_profile.json` sidecar; the default listing remains
normal drive sessions only.
| `POST /control/obd_request` | one-shot read-only OBD/UDS request — `{service, identifier?, header?, timeout_seconds?}`, whitelisted to Mode 01/02/03/06/07/09/0A/22 |
| `GET /control/tuning-mode` / `POST /control/tuning-mode` | inspect / arm the focused high-rate tuning poll profile (narrows Mode 01 to a tuning channel set, suspends DID windows) |
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
| `export_dataset` | `scripts/export_dataset.py` — the Export panel's backend (Task 5.6b) |
| `did_sweep` | `tools/did_sweep.py` — one Mode 22 DID sweep pass, tagged with a label; writes `<job_id>.did_sweep.json` |

Job parameters are structured JSON, not shell text. Common parameters include
`days`, `all`, and `session_ids`. LSTM jobs also accept `device`; behavior
refits accept `recipes` and `dry_run`; `export_dataset` requires
`session_ids` (at least one) and also accepts `channels`, `resample_hz`,
`include_tags`, `exclude_tags`, `formats` (default `["csv"]`), `purpose`,
`zip` (default `true`), `out_dir`, and `name`. `did_sweep` requires `start`/
`end` (Mode 22 DIDs, int or hex string) and `label`; optionally `header`
(default `7E0`), `session` (`"auto"` resolves from `data/dids.yaml`,
`"none"` forces bare polling, or an explicit hex sub-function), `keepalive`,
`delay_seconds`, and `read_timeout`. `GET /control/jobs/{id}/result` reads
the snapshot back once the job succeeds (same convention `replay_window`
uses) — see [DID sweep + diff](#did-sweep--diff) below for the two-sweep
comparison workflow.

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

## Passive mode

CANary's broadcast capture is listen-only by design (FR-02 / NFR-06), but the
OBD side is not: the scheduler transmits tester requests (Mode 01/03/06/07/09/
0A polls, Mode 22 DID reads, diagnostic session control) and ISO-TP flow
control. That is normally harmless — an ECU answers diagnostic requests all
day.

It stops being harmless when another tool owns a diagnostic session with a
module: an ECU reprogramming run holding a programming session, a
bidirectional actuator test, a scan tool mid-routine. Passive mode is the
switch for those windows. The daemon keeps running — broadcast CAN logging,
storage, GPS, anomaly scoring, dashboard — but transmits nothing.

```bash
# arm before the other tool connects
curl -sX POST http://canary.local:8000/control/passive \
  -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' -d '{"enabled": true}'

# or arm with a TTL — auto-disarms after 300s even if you never come back
curl -sX POST http://canary.local:8000/control/passive \
  -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' -d '{"enabled": true, "ttl_seconds": 300}'

# confirm (also visible as `passive` in GET /status)
curl -s http://canary.local:8000/control/passive \
  -H "Authorization: Bearer $CANARY_CONTROL_TOKEN"

# disarm when the other tool is done
curl -sX POST http://canary.local:8000/control/passive \
  -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' -d '{"enabled": false}'
```

MCP equivalent: `set_passive_mode(enabled=true|false, ttl_seconds=...)`. To
boot the daemon already passive, set `CANARY_PASSIVE=1` in its environment.

**`ttl_seconds` (capped at 3600) is the fix for "I armed it and forgot to
disarm."** A background daemon task (`poll_passive_ttl`) polls for expiry
every couple of seconds and flips the gate back off on its own — no
depending on a session staying alive, a script reaching its `finally`
block, or an operator remembering. Omit it for the old
arm-until-you-say-so behavior. `GET /status`'s `passive.ttl_remaining_seconds`
shows the countdown (`null` when no TTL is set).

**Expect OBD to go stale while armed.** `vehicle_state` will read
`engine_off`/`no_data` and behavior models stop scoring, because nothing is
polling. That is the mode working, not a fault — `GET /status` reports
`passive.enabled` plus a `suppressed_frames` counter so it is distinguishable
from a real bus problem.

Two interactions worth knowing:

* **Scheduler hygiene.** The pause happens where requests are *issued*, not at
  the send, so no request accrues a timeout and nothing enters the miss-backoff.
  Passive mode hands back an undegraded scheduler when it lifts.
* **Record arming.** The `disarm_after_seconds` silence timer is suspended
  while passive. That timer infers "vehicle is parked" from an absence of
  `can`/`obd` records, and during a passive window the inference is invalid —
  we stopped polling ourselves, and reprogramming tools routinely issue
  `CommunicationControl`/`ControlDTCSetting` to quiet normal bus traffic during
  erase/write. Without the suspension, recording would disarm during exactly
  the window passive mode was armed to capture.

### Hardware-level listen-only

For a guarantee no software path can defeat, put the controller itself in
listen-only mode on the Pi — it then cannot drive the bus at all, not even ACK
bits:

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 listen-only on
sudo ip link set can0 up
```

Reverse with `listen-only off`. This needs an interface cycle, so it is a
deliberate pre-procedure step rather than a runtime toggle; use it *in
addition to* passive mode when the stakes justify it (an ECU reflash does).

## Raw CAN activity capture

`POST /control/can_activity` listens for N seconds (capped at 30) and
summarizes, per arbitration id, how often it appeared and which byte
positions actually varied. Pure receive — no frames are transmitted, no
interaction with passive mode. This is the reverse-engineering workhorse:
bracket a physical action (press the brake, flip a switch — see
[Tag physical actions, not graph selections](../AGENTS.md) for why it has to
be the action, not an NT510 graph selection) and `changed_byte_indices` on
whichever id lit up is the answer to "which ID carries that signal" —
without an offline `candump` capture and a hand-written byte diff.

```bash
curl -sX POST http://canary.local:8000/control/can_activity \
  -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"seconds": 5}'
```

```json
{
  "can_activity": {
    "duration_seconds": 5.0, "frames_captured": 4213, "truncated": false,
    "ids": [
      {"id": "0x180", "frame_count": 501, "hz": 100.2,
       "first_hex": "...", "last_hex": "...", "changed_byte_indices": [2, 5]}
    ]
  }
}
```

`ids` (a list of arbitration ids) restricts the capture — useful for a
tighter follow-up window once a first wide capture has narrowed the
candidates. MCP equivalent: `can_activity(seconds, ids)`.

## Ad-hoc OBD/UDS requests

`POST /control/obd_request` sends one read-only diagnostic request and
returns whatever comes back, instead of waiting for the poll scheduler's
cadence — useful for probing a specific Mode 06 MID or Mode 22 DID, or
reading a Mode 09 InfoType (VIN, calibration ID, CVN), without arming a
capture or writing a one-off script. Whitelisted to Mode
01/02/03/06/07/09/0A/22 (`CanObdClient.query`'s allowlist, enforced
independent of this route).

```bash
# calibration ID (Mode 09, InfoType 0x04)
curl -sX POST http://canary.local:8000/control/obd_request \
  -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"service": 9, "identifier": 4}'

# a specific Mode 22 DID, addressed to the engine ECM (0x7E0)
curl -sX POST http://canary.local:8000/control/obd_request \
  -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"service": 34, "identifier": 4353, "header": 2016}'
```

Response shape:

```json
{
  "obd_request": {
    "service": "0x09", "identifier": "0x0004", "request_id": "0x7DF",
    "responded": true, "responder_id": "0x7E8",
    "raw_hex": "...", "decoded": {"ascii": "..."}
  }
}
```

`responded: false` means no answer arrived within `timeout_seconds`
(default 2 s, capped at 10 s) — indistinguishable on the wire from "this ECU
doesn't support it," so treat it as "no answer," not an error. `decoded` is
populated when a Mode 01/02 PID, Mode 03/07/0A DTC list, Mode 09 ASCII
payload, or catalogued (`data/dids.yaml`) Mode 22 DID matches; otherwise only
`raw_hex` is set. MCP equivalent: `obd_request(service, identifier, header,
timeout_seconds)`.

This transmits like any other OBD request, so the same [passive
mode](#passive-mode) caveat applies: arm it first if another tool currently
owns a diagnostic session with the target module.

## DID sweep + diff

The interactive `tools/scan_constant_dids.py` (multi-pass, pauses between
passes so you can change engine state by hand) is the right tool at the
vehicle. The `did_sweep` job is its unattended sibling: one sweep pass over
a DID range, tagged with a label, submitted like any other background job —
turning a knock hunt's "sweep at idle, sweep under load, diff" into two
`POST /control/jobs` calls and a read instead of an SSH session.

```bash
# sweep once per condition, distinct labels
curl -sX POST http://canary.local:8000/control/jobs \
  -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" -H 'Content-Type: application/json' \
  -d '{"kind": "did_sweep", "params": {"start": "0x1100", "end": "0x12FF", "label": "idle"}}'

curl -sX POST http://canary.local:8000/control/jobs \
  -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" -H 'Content-Type: application/json' \
  -d '{"kind": "did_sweep", "params": {"start": "0x1100", "end": "0x12FF", "label": "wot"}}'

# once both report status=succeeded (GET /control/jobs/{id}):
curl -s "http://canary.local:8000/control/jobs/<idle-job-id>/result" -H "Authorization: Bearer $CANARY_CONTROL_TOKEN"
```

MCP equivalent: `start_job(kind="did_sweep", params={"start":..., "end":...,
"label":...})` twice, then `diff_did_snapshots(job_a, job_b)` — no separate
HTTP round trip for the diff itself; the MCP tool reads both result files
directly (same local-filesystem assumption `replay_window` already makes)
and classifies every DID that answered in either run:

* **`varying`** — value differs between the two runs; the candidates worth
  investigating.
* **`constant`** — answered in both runs, same value both times.
* **`intermittent`** — answered in one run but not the other (a
  session/timing flake, or a DID genuinely gated on the condition).

Each entry carries the raw hex from each snapshot it appeared in — most DIDs
have no `data/dids.yaml` scale/offset pinned yet, so raw bytes are the
honest thing to hand back rather than a guessed decode.

`did_sweep` is Canable-only (no ELM327 branch — the job runs inside
`JobController`'s subprocess against the daemon's own CAN interface) and
read-only: only Mode 22 reads and, when `session` resolves to something,
Mode 0x10 session control (no memory write, no actuation) are ever sent.
`session="auto"` (the default) resolves the gating sub-function from
`data/dids.yaml`'s access block, matching what the live scheduler's DID
windows already use for this vehicle.

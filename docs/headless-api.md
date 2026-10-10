# Headless API

The Raspberry Pi runs one CANary daemon. That daemon collects telemetry,
writes Parquet, runs model polling/inference, serves the dashboard, and owns
the authenticated control API used from a laptop.

The public dashboard GET endpoints are unauthenticated in v1. The
`/control/*` endpoints require a bearer token from `CANARY_CONTROL_TOKEN`.

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
| `GET /status` | storage path/free space, latest session, loaded model versions, vehicle, anomaly counters; running calibration (`ecu_calibration_id`, `ecu_calibration_read_at`, `calibration_matches_registry` true/false/null, `calibration_alert`) and `tuning_readiness` chips `{ok, value, detail, fix}` (W02); `startup` — when this daemon started recording (S01.1, below). `?view=compact` is the pollable projection and carries `startup` too |
| `GET /config` | JSON view of the loaded `canary.yaml` |
| `GET /telemetry/latest?n=N` | newest `N` telemetry rows from disk; `decoded:` rows are decoded on read from the raw CAN frames (P02.8: none is stored since 2026-09-26), exactly as a session recorded before then stored them. `per_channel=true` gives the latest row per channel (the dashboard seed and the Parquet `vehicle_state`) |
| `GET /anomalies?since=...&limit=N` | persisted anomaly events from JSONL; `since` and `limit` skip whole daily files, so pass one — with neither, the daemon parses the entire history |
| `GET /bindings` | decoded CAN binding metadata for display |
| `GET /dids` | Mode 22 DID catalog metadata |
| `GET /models/behavior` | behavior-model surfaces for dashboard panels; per model `serving_status` (`state`, `reasons`, `lifecycle` validated/unvalidated/exploratory/demoted/quarantined, typed `permitted_uses`, governing `policy` entry), `artifact` identity (version, fingerprint, fitted_at, tune) and `validation_scope`; top-level `policy` names the profile `behavior_policy.yaml` |
| `GET /models/behavior/live?series=` | per-model expected/actual/deviation live view (`series=false` drops the rolling trail); each model also carries `reason`, `status`, `lifecycle`, `permitted_uses`, `artifact_version` and `abstain_reason` (why no trusted verdict this tick) |
| `GET /obd/coverage` | OBD PID/DID coverage summary |
| `GET /can/coverage` | broadcast CAN ID coverage summary |
| `GET /test-profiles` | available UI test profiles |
| `GET /obd/dtc` | cached stored/pending/permanent DTC report, MIL state, staleness |
| `GET /obd/monitors` | cached Mode 06 on-board monitor sweep — per-cylinder misfire counts, raw monitor test results (value/min/max, pass/fail), staleness |
| `GET /sessions/{id}/trip-summary` | trip-summary sidecar (duration, distance, fuel/economy/CO2, idle time/fraction, max/avg speed, 0-60, 1/4-mile) — computed on demand from the stored session on first request, cached to `trip_summary.json` after |
| `GET /analysis/binned?value=&x=&y=&x_bins=&y_bins=&agg=&session=&since=&until=&units=` | ad-hoc binned aggregation over the DuckDB parquet layer ("trims by rpm×load") — `agg` is `mean`\|`median`\|`p95`\|`count`; `y`/`y_bins` optional (1D when omitted); `units` is `stored` (default) or `engineering`, the latter rescaling `decoded:` channels via `bindings.yaml` (they are raw counts on purpose — `decoded:ECM_RPM` is 8 counts/rpm). `decoded:` channels of `confirmed` and `persist: true` bindings are decoded on read from the raw CAN frames, in the query (P02.8: no `decoded` row is stored since 2026-09-26) |

### `/status.startup` — when recording began (S01.1)

Seconds from the **process start** (read from `/proc/self/stat`: the unit's
own start timestamp is not readable as the `canary` user, and its `ExecStart`
is this process) to each startup stage. Each stage is recorded once; `null`
means not reached (yet).

```json
"startup": {
  "clock": "seconds from the process start in /proc/self/stat; ...",
  "process_start": {"at": "2026-09-22T16:32:05.120000+00:00", "source": "proc_self_stat"},
  "stages": {
    "main_entered": 9.8, "profile_resolved": 11.9, "canary_starting": 11.9,
    "daemon_imported": 13.1, "components_built": 14.0,
    "collector_tasks_created": 14.2, "first_record_stored": 14.9,
    "live_source_ready": 28.3, "models_polled": 51.0,
    "app_built": 25.7, "http_bound": 25.9
  },
  "first_record_stored_by_source": {"can": 14.9, "obd": 16.2},
  "first_record_timestamp_by_source": {"can": "2026-09-22T16:32:20.01+00:00", "obd": "..."},
  "model_loaded": {"isolation_forest": 50.8},
  "skipped": {},
  "pending": [],
  "complete": true
}
```

(Numbers illustrative.) `first_record_stored` is marked by the storage sink
after the writer accepted the record, never by a collector — a log line saying
"collectors started" once ran 25 s ahead of any recording. It is the time the
record was handed to the Parquet writer, so the session's first parquet row
lands within one flush interval of it. `process_start.source` is
`timeline_created` where there is no procfs. A live source whose build failed
appears under `skipped`, not `pending`. When `pending` empties the daemon logs
one `startup complete: first record stored N s after process start` line.


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
| `GET /control/capture/quality` | capture-quality v2 verdict for the running capture profile, scored arm-to-now on what has persisted (`scope: live`); `batching` counters are arm-to-now deltas (`batching.scope: capture`), never the scheduler's lifetime totals; `failing_constraints` names what bound. Scored in a child process (it reads the rows back); `503` if that child cannot answer |
| `GET /control/capture/quality/{session_id}` | recompute coverage and tuple yield from a stored capture session's rows (never rewritten), returned beside its stored sidecar (`scope: stop`, or `trailing_snapshot` for v1 files); also scored in a child process (`503` if it cannot answer); see `docs/capture-quality.md` |
| `GET /control/logs?include_sessions=&limit=` | Engine Tuning capture browser (W04.1): captures (plus ordinary sessions when asked) newest first by parsed start time, each with profile, duration, row count (Parquet footers only), tune revision + source, flash boundaries inside the log, quality verdict and `end_reason` |
| `GET /control/sessions/{session_id}/frame?specs=&grid=&regime=&offset=&max_rows=` | one log as a wide frame (W04.3): channels on one clock via `read_aligned` (a row only where every channel was sampled in the cell; `freshness.dropped` counts the rest), per-column unit, `regime` = `all`\|`closed_loop`\|`enrichment`\|`fuel_cut`\|`idle` (an unavailable regime names the missing channel), tags and revision boundaries; capped at 20,000 rows per page with `capped` stated |
| `GET /control/sessions/{session_id}/cell-residency?table=` | per-cell sample count and dwell seconds for one log on a ROM table (W05.6), from the ECM's map-trace channels against the axes of the revision the log ran; nearest-breakpoint cells, compared on the breakpoints each axis's mapping names (`axis_units`: `raw` undoes the channel's catalog decode and lands on the ROM's raw axis bytes, as the fuel-target X trace does since W05.1). Past an axis end a sample goes to the edge cell when the table declares the ECM clamps (`edges: clamp`, counted in `clamped_samples` / `clamped`), otherwise it is counted in `outside_axes`. Same child, deferral and row budget as `/frame`. Anything that cannot answer is `available: false` with a named `reason` — `axis_mapping_unverified` (the vehicle's `rom_viewer.yaml` has not settled how a trace lands on the axis; refused before any read), `not_captured` (the log never polled the trace channels; `missing` names them), `revision_unknown`, `axes_not_recorded`, `raw_axes_not_recorded`, `stored_decode_unknown`, `stored_not_whole_counts` (the log's values do not return to whole raw counts under the catalog's current decode), `table_axes_unverified`, `no_trace_mapping` — never an empty grid |
| `GET /analysis/delivery-error?sessions=&revision=&measured=&min_load=` | delivery error by rpm band pooled over sessions (W04.7) — each session paired by acquisition time and gated on its own clock (R05.16, `engine_version: 2`, `pairing` block; bands carry `p05_pct`/`p95_pct`), band-averaged once; `measured` names the source (`obd:O2_WR_LAMBDA_S1` or `semantic:measured_afr_b1`/`_b2`); carries `noise_floor` with its `conditions`, `gates`, `conditions`, and `unavailable_kind` (`not_captured` vs `gates_removed_everything`) |
| `GET /analysis/trim-surface?sessions=&revision=&rpm=&airflow=&min_n=` | closed-loop STFT + LTFT per bank by rpm x airflow, pooled and per session |
| `GET /analysis/timing?sessions=&revision=&timing=&rpm=&airflow=&min_n=` | final timing per rpm x airflow cell |
| `GET /analysis/pull-comparison?revision_a=&revision_b=&value=&floor_channel=` | a value per cell, revision A vs B; `dropped` flags a drop in a cell A had advanced |
| `GET /analysis/dyno?sessions=&revision=&road=&payload_kg=&induction=&ambient_temp_c=&allow_single_direction=` | driving dyno (H07.9): wheel power and torque against rpm per session and gear, through the workbench child (deferred while a capture records, `analysis_busy` while another child runs). `prerequisites` lists every profile fact power rests on as `recorded`/`stated`/`estimated`/`pending`/`missing` with what owes it; a `pending` road load or squat refuses every run `coefficients_pending`, absent configuration (no WOT criterion or usable `operator_demand` provider, a mass without `mass_evidence`) `prerequisites_missing` — no coefficient is defaulted. `sessions[].windows` lists every rpm rise found with its gear and heading or its refusal; each `runs[]` entry is a curve (`curve`, `per_heading`, `conditions`, `flags` incl. the pitch offset and its source, `uncertainty.noise_floor` per quantity or its `no_floor` refusal, `uncertainty.heading_spread`) or a named refusal. `road`, `payload_kg` and `induction` (`na`\|`boosted`) are stated by the caller and a run without them is refused naming each; `ambient_temp_c` only when the session has no ambient channel. `unavailable_kind`: `not_configured`, `prerequisites_missing`, `coefficients_pending`, `over_budget`, `no_pull_found`, `every_pull_refused`, `every_run_refused`. A pool whose stored rows cannot be read within the analysis limit at the measured Pi rate is refused up front: 409 `analysis_refused`, `reason: dyno_over_budget` (~23.9 M rows at the default 900 s) |
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
| `POST /control/replay` / `POST /control/replay/stop` / `GET /control/replay` | run a stored session through the live pipeline with isolated `_replay-*` outputs. A `loop: false` replay ends on its own when its pass runs out (writer back to live, live input reopened); the idle status keeps that replay's id and final counters with `ended` = `completed` / `stopped` / `failed`. Stop after a replay already ended returns that final status (200) and publishes another `replay.stop` control event; 409 only when no replay has run since the daemon started. A stop lands within one chunk load. Start answers 422 when a change-only session (P02.10) cannot be replayed faithfully: slower than real time, or recorded with a longer `collection.broadcast_stale_timeout_seconds` than the daemon's |
| `GET /control/profiles` / `POST /control/profiles/{slug}/name` | list vehicle profiles / set a profile display name. The listing also carries each profile's `tracked` flag and `overrides` (fields this machine's overlay sets, `parameters.<name>` for parameters), `in_use` (the `vehicle:` block the daemon is running, copied at startup), `pending_restart` (active-profile fields whose registry value the daemon does not run yet), `resolution` (how this start chose its profile: `how`, `vin_read`) and `parameter_fields` (label, unit, kind, evidence field per vehicle parameter) — see [Vehicle profile editing](#vehicle-profile-editing) |
| `PATCH /control/profiles/{slug}` / `POST /control/profiles/{slug}/revert` | edit a profile's `name`, `make`, `model`, `year`, `vin` and partial `parameters` / put `fields` (or every override) back to the tracked registry's values — see [Vehicle profile editing](#vehicle-profile-editing) |
| `POST /control/vin/read` | read the connected vehicle's VIN (Mode 09 0x02) through the running OBD collector; returned for confirmation, never saved — see [Vehicle profile editing](#vehicle-profile-editing) |
| `GET /control/power` / `POST /control/power/keepalive` | UPS watchdog status / set or cancel the keep-alive hold |
| `GET /control/passive` / `POST /control/passive` | inspect / arm the CAN transmit gate (receive-only mode) — see [Passive mode](#passive-mode) |
| `POST /control/trigger_poll` | force specific channels due on the OBD scheduler's next tick — `{channels}` — instead of waiting out their tier cadence |
| `POST /control/can_activity` | capture N seconds (capped at 30) of raw CAN traffic — `{seconds, ids?}` — per-id frame rate + which byte positions changed |
| `GET /control/obd_discovery` / `POST /control/obd_discovery/reset` | cached OBD support-discovery state per vehicle / invalidate it so the collector re-walks the Mode 01 / Mode 06 support groups on its next budget slots — see [docs/diagnostic-discovery.md](diagnostic-discovery.md); live progress is `/status.obd_scheduler.discovery` |
| `GET /control/capture/profiles` | list strict, vehicle-scoped capture profiles; `mode22_rate_weights` maps selected DID names to relative cadence (missing members default to 1); `memory_rate_weights` does the same for selected memory channels, and a memory window (one 0x23 read) runs at the largest weight among its selected members |
| `PUT /control/capture/profiles` / `DELETE /control/capture/profiles/{name}` | save or delete a capture profile (`default` cannot be deleted); DID weights must be finite, positive, and name selected `mode22` members. Memory weights are refused (422) unless each is finite and positive and names a channel selected in `memory:`; a key naming a memory *window* is refused with its member channels listed, because a window's rate comes from its members; a selected channel the catalog does not hold is refused (409) as `memory:` always was |
| `POST /control/capture` / `DELETE /control/capture` | start a named isolated capture / return to default collection; start returns capture session path and budget state, stop returns `ended_capture_dir` and restores the exact pre-capture budget. Retrying an identical active start or immediately repeated stop returns the existing result with `idempotent: true`; that stop-response cache does not survive a daemon restart. |
| `POST /control/rom/inspect` | authenticated, read-only ROM inspection: raw `application/octet-stream`, maximum 2 MiB. Decodes in memory, validates source hash/CRCs/table evidence, returns maps, axes, verified live-channel providers, each available table's `trace` (its X/Y trace channels, `axis_mapping` state + evidence, and the scheduler `request_channel` carrying each) and explicit TBD entries. Does not persist the file, change the active tune, or communicate with the ECU. |
| `GET /control/ecu/application-model` | authenticated, read-only coverage of the ECM application twin (E05.1), from the profile's `ecu_application_model.yaml`: `model` (name, schema/model version, file sha256), `program` (identity hash and spans, UpRev patch, listed images), `image` (the revision recorded as running now: `known`, `revision`, `role`), the fuel-task `schedule`, and `outputs[]`. Each output has `role` (`final`, or `intermediate` with the output it `feeds`: E05.4's timing-trim lookup is an intermediate of `ignition_timing`, never timing itself) and `status` (`qualified` only with no blockers and an observation channel; `unobserved` with no blockers but no channel; else `conditional`), its `path` of nodes (each with any E05.3 `reference_model` and its `reference_validation`) and the same nodes `by_role` (`implemented` / `observed` / `assumed` / `not_modelled` / `calibration_dead`), `blockers` with their nodes, `equals_output_when`, and `regions`. Also `uncovered[]`, the logic the trace reached but did not resolve. 404 when the vehicle declares no model. Does not communicate with the ECU. |
| `GET /control/brain/status` | the second brain's standing (E04.6): `identity` (program id/status, calibration, image), `change`/`last_change` against what it last recorded, `program_specific_state` (invalid after a program change or an unverified program), `predictions` (re-based on a calibration change), `tables` (the twin's table source now), `ram_addresses` (`reverify` only on a program change; a calibration flash leaves them `unchanged`), `transient` (always `unknown` after a restart), `memory` counts, `jobs` counts. Read-only: never records a change. MCP `get_brain_status` |
| `GET /control/brain/memory` | `MemoryQuery` over the vehicle's investigation memory: `kind` (comma list), `incident`, `subject`, `session`, `regime`, `calibration`/`program_id` (`unknown` = recorded unknown), `valid_at`/`valid_before`/`valid_after`/`as_of` (ISO with offset; 400 without), `text`, `history`, `limit`. MCP `query_memory` |
| `GET /control/brain/evidence/{id}` | one evidence id resolved (404 unknown, 400 wrong kind), with `part`: `ecu_computation` (the twin) or `engine_response`, and its recurrence count. MCP `resolve_evidence` |
| `GET /control/brain/investigations/{id}/card?as_of=` | the deterministic E01.5 investigation card plus `parts`, its evidence ids split into ECU computation and measured engine response. MCP `get_investigation_card` |
| `GET /control/brain/twin/sessions/{session}` | the target-phi twin's evaluation of a stored session: the job, per-slot `evaluations` (accounting, `missing_inputs`, `capture_requirement`, `trace` of observed/predicted phi with each sample's state), refusals, and `currency` for the program in force. `state: not_evaluated` before a job ran. MCP `get_twin_session_evaluation` |
| `GET /control/brain/jobs` / `GET /control/brain/jobs/{id}` | idle-time brain jobs with `state` (`queued`/`running`/`deferred`/`pending`/`completed`/`failed`) and its named `reason`; one job with its full result. MCP `list_brain_jobs` / `get_brain_job` |
| `POST /control/brain/jobs` | queue `{kind: twin_session_evaluation \| memory_consolidation, params, rerun?}` (202); the job id is the kind plus canonical params, so the same work is one job. Runs in the digest's bounded child only while collection is inactive (`vehicle_state` `parked` or `no_data`, no capture, no replay, no digest or workbench child), yields when collection resumes; an open session stays `pending` until its trip-end digest or a newer session exists. The daemon also queues one itself after each trip-end digest when the session's capture manifest recorded the twin's command word. While a brain child is alive the workbench routes answer 409 `analysis_busy` / `brain_child_yielding` and the child yields. 404 unknown session, 400 no twin definition |
| `POST /control/rom/library?filename=<name>` | ROM library upload (W08.3): raw `application/octet-stream`, 2 MiB cap (413). Accepts a plain image of the vehicle's exact size (1,048,576 bytes on the Xterra) or an `.OsirisROM`; the UpRev encrypted backup `.bin` and anything else are refused by name (422). Stored per vehicle under `daemon.rom_library_dir`, content-addressed by the plain image's sha256; the same image again keeps one entry and adds the source. Returns the entry plus `created`. See [ROM library and ECU backup](#rom-library-and-ecu-backup-w08). |
| `GET /control/rom/library` / `GET /control/rom/library/{sha}` | entries newest first (`{entries, count}`) / the full W08.2 description of one image plus its `entry` |
| `GET /control/rom/library/{sha}/download` | the plain image, `application/octet-stream`, `Content-Disposition: attachment; filename="<label>-<sha8>.bin"` (no `.OsirisROM` export) |
| `PATCH /control/rom/library/{sha}` / `DELETE /control/rom/library/{sha}` | set the label (`{"label": str \| null}`, the only editable field) / remove the entry |
| `GET /control/rom/library/{sha}/tables` | every mapped table in every map slot: raw cells, decoded cells where a decode is declared, axes, confidence and evidence (W08.4) |
| `GET /control/rom/compare?a=<sha>&b=<sha>` | changed byte ranges a -> b merged with a 16-byte gap, each labelled with the regions and tables it overlaps; table ranges carry per-cell old/new values, decoded where declared (W08.5) |
| `GET /control/rom/backup` / `POST /control/rom/backup` / `POST /control/rom/backup/cancel` | ECU flash backup job (W08.7), read-only: state + live preconditions / start (202, or 409 naming every refusing precondition) / cancel a running read (409 when none runs) |

The frame and every `/analysis/*` route in this table (W04) run in the digest's bounded single-flight child and refuse while a capture is recording: `409 {"error": "analysis_deferred", "profile", "message", "laptop_path"}`. A second, different analysis (or a running digest) is `409 analysis_busy`; a child failure is `503 analysis_unavailable`. Calculated load is refused as an axis.

`GET /control/sessions?captures=true` lists only `_capture-*` sessions and
tags each from its `capture_profile.json` sidecar; the default listing remains
normal drive sessions only.
| `GET /control/diagnostics/functions` / `POST /control/diagnostics/read_codes` / `POST /control/diagnostics/clear_codes` | diagnostic functions the vehicle profile records; read or clear one K-line module's stored codes — `{module}` (see [Diagnostic functions](#diagnostic-functions)) |
| `POST /control/obd_request` | one-shot OBD/UDS request — `{service, identifier?, header?, timeout_seconds?}`; `service` accepts any byte, including state-changing and actuation services. CANary must never use it to write ECU non-volatile memory or reflash a module |
| `GET /control/tuning-mode` / `POST /control/tuning-mode` | inspect / arm the focused high-rate tuning poll profile (narrows Mode 01 to a tuning channel set, suspends DID windows) |
| `GET /control/feedback` / `POST /control/feedback` | read / record anomaly-feedback verdicts |
| `GET /control/training-runs` | training-run ledger |
| `GET /control/vehicle-ledger` / `POST /control/vehicle-ledger` / `DELETE /control/vehicle-ledger/{id}` | vehicle maintenance/intervention ledger. POST body `{occurred_at, kind, title, details?, odometer_km?, affected_channels?, corpus_boundary?, event_id?, tyres?, intervention?}`; `intervention` is `hardware` \| `fuel` \| `instrumentation` (E01.4: the change is recorded into investigation memory as an intervention; omitted = unclassified). `event_id` replaces that entry whole, so a correction re-states `intervention` or erases it |
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

## ROM library and ECU backup (W08)

Read-only throughout: these routes store and describe flash images and read
the ECU's flash. Nothing writes the ECU. The vehicle's image layout is data
(`<profile>/rom_map.yaml`, validated by `src/analysis/rom_map.py`; every
region and table carries `confidence` = `verified` / `inferred` / `unknown`
and required `evidence`), and so is the read recipe
(`<profile>/rom_backup.yaml`). Addresses in payloads are integers (byte
offsets into the plain image).

**Library entry** (`POST` response, `GET /control/rom/library` `entries[]`,
`PATCH` response):

```json
{
  "sha256": "a46baa24...be50", "crc32": "95b993c5", "size": 1048576,
  "container_sha256": "03d00051...28a2",
  "source": "upload_osirisrom",
  "sources": ["upload_osirisrom", "ecu_read"],
  "source_records": [{"source": "upload_osirisrom", "filename": "ZS44A-Tuned-20260920.OsirisROM",
                      "added_at": "...", "container_sha256": "...", "read_meta": null}],
  "filename": "ZS44A-Tuned-20260920.OsirisROM", "label": null,
  "added_at": "...", "last_added_at": "...",
  "calibration_id": "1ZS44A00000",
  "checksum": {"ok": true, "stored": {"sum": "0x...", "xor": "0x..."},
               "computed": {"sum": "0x...", "xor": "0x..."}, "reason": null,
               "algorithm": "be32_sum_xor"},
  "revision_match": {"revision": "ZS44A-20260920", "status": "matched", "reference": null},
  "map_reason": null,
  "read_meta": null
}
```

`source` is the first source; `sources` every distinct source, oldest first.
`revision_match.status` is `matched` (a flashed revision's `image_sha256` in
`tune_revisions.yaml`), `reference` (a never-flashed known image, e.g. the
UpRev base, named in `reference`), `unrecognised`, or `unknown` when the flash
history could not be read (`reason` says why; the image was never compared). `checksum` is `null`, and
`map_reason` says why, when no map definition covers the image's calibration
ID. A failing checksum is reported, never refused. `read_meta` is the latest
ECU read's metadata (below), `null` for uploads.

**Description** (`GET /control/rom/library/{sha}`): `sha256`, `crc32`,
`container_sha256`, `calibration_id`, `size`, `checksum`, `revision_match`,
then

- `blocks`: 256 entries `{offset, ff_pct, entropy, class}`, one per 4 KiB;
  `ff_pct` 0-100, `entropy` bits per byte, `class` `erased` / `sparse` (>= 50 %
  0xFF) / `dense` (>= 6 bits/byte) / `data`;
- `regions`: `{name, start, end, size, kind, confidence, added_by_uprev,
  evidence, slot}`, sorted by start, nested regions after their parent; map-slot
  spans resolved per slot (`slot` set, name `Map n: ...`);
- `tables`: one per table placement `{id, kind, name, label, slot, offset, end,
  rows, cols, dtype, role, decode, axes: {rows, cols} (table ids), confidence,
  evidence}`;
- `map_slots`: `{base, stride, count, end, confidence, evidence, slots: [{slot,
  start, end}]}`;
- `map`: `{definition, calibration_id, calibration_ids, image_size,
  erase_sectors: [{index, start, end, size, confidence, evidence}], checksums}`;
- `map_reason`: `null`, or e.g. `"no map definition for calibration 1ZS04C00000"`
  (then `map`, `map_slots` and `checksum` are `null`, `regions`/`tables` empty;
  identity and `blocks` are still there);
- `entry`: the library entry above.

**Tables** (`GET /control/rom/library/{sha}/tables`): `{sha256, calibration_id,
map_slots: [1, 2, 3, 4, 5], tables: [...], reason}`. Each table:

```json
{"id": "fuel_targets", "kind": "fuel_targets", "name": "Fuel Targets",
 "label": "Fuel Targets (Map 1)", "slot": 1, "offset": 918048, "end": 918111,
 "rows": 8, "cols": 8, "dtype": "u8", "role": "table", "unit": "AFR",
 "raw": [[128, 128, ...], ...], "decoded": [[14.7, 14.7, ...], ...],
 "decode": {"formula": "afr = 1881.6 / raw", "kind": "reciprocal", "quantity": "afr",
            "unit": "AFR", "confidence": "verified", "evidence": "..."},
 "axes": {"rows": {"id": "fuel_targets_rpm_axis", "name": "...", "offset": 51877,
                   "raw": [24, 32, ...], "decoded": [1200.0, 1600.0, ...], "unit": "rpm",
                   "decode": {...}, "confidence": "verified"},
          "cols": {"id": "fuel_targets_bfs_axis", "raw": [52, 68, ...], "decoded": null, ...}},
 "confidence": "verified", "evidence": "..."}
```

A table without a declared decode (`K`, `be16` at slot +0x762, `inferred`)
has `decoded: null` and is shown raw; a scalar is `[[value]]`. `role` is
`axis` for a table another table uses as an axis. `slot` is `null` for a
single-copy table.

**Compare** (`GET /control/rom/compare?a=&b=`): `{a, b, changed_bytes, gap: 16,
ranges, map_applied, map_reason}`; each range `{start, end, length, changed,
labels: [str], overlaps: [{type: table|checksum|slot_region|region, id, label,
slot, confidence}], cells: [{table, table_label, slot, row, col, address, old,
new, old_decoded, new_decoded, unit}]}`. `end` is inclusive and is the last
changed byte; `changed` counts differing bytes. Labels are tables first, then
checksum words, then regions innermost first. The map labels ranges only when
both images' calibration IDs are mapped.

**Backup** (`GET /control/rom/backup`, and the `POST` responses; `...` marks values not yet measured on the truck, W08.9):

```json
{"state": "running", "phase": "reading", "reason": null,
 "blocks_done": 5120, "blocks_total": 16645, "bytes": 322560, "bytes_total": 1048576,
 "elapsed_s": ..., "eta_s": ..., "retries": 0, "requests": ..., "timeouts": 0,
 "send_errors": 0, "negative_responses": {}, "started_at": "...", "ended_at": null,
 "stored": false, "sha256": null, "verified": null, "integrity": null, "revision_match": null,
 "polling_resumed_s": null, "polling_resume_note": null,
 "preconditions": null, "can_start": false, "blocked_by": "a backup is running"}
```

`state` is `idle`, `refused` (with `reason`), `running`, `done` (image stored
and verified: `stored: true`, `sha256`, `revision_match`), `failed` (with
`reason`) or `cancelled`. `stored` says whether the image is in the library,
and `sha256` is set only when it is: an image that read completely but failed
integrity is still stored, marked unverified (`reason` starts `unverified:`,
`stored: true`), while a read that finished but could not be written is
`failed` with `stored: false`, no `sha256`, and a reason ending "nothing was
kept". `send_errors` counts frames the bus itself refused (`can.CanError`);
they are retried like timeouts and named "CAN send failed", never confused
with the transmit gate suppressing a frame. `integrity` is
`{checksum, calibration_id: {image, ecu, ok}, reread: {blocks, mismatches}}`,
or `null` if the checks themselves failed. While not
running, `preconditions` lists every start check `{id, ok, detail}` — ids
`backup`, `recipe`, `obd_source`, `vehicle_state`, `engine_watch`, `passive`,
`foreign_tester`, `ecm_session`, `capture` — with `can_start` and `blocked_by`.
`polling_resumed_s` is the time from releasing the ECM session to the first
OBD reading after it (`null` with `polling_resume_note` if none came within
60 s).

The read refuses unless `vehicle_state` is `ignition_only` **and** the fresh
broadcast engine speed is below the running threshold (400 rpm; the classifier
alone prefers a fresh OBD rpm, whose last `0` stays fresh for up to 10 s after
polling pauses), a fresh broadcast engine speed is available to watch (OBD
polling pauses for the read), passive is off, no other tester has been heard
within 120 s or holds the ECM session, nothing else holds the session, and no
capture window or profile capture is active; it re-checks these during the
read and aborts on an engine start, judged the same way. It
takes the ECM session as holder `rom_backup` (`/status.passive.ecm_session_owner`
reads `rom_backup` meanwhile, and the OBD loop pauses), sends the recipe's
session sequence and `23 [BE32 addr] [BE16 len]` reads with a per-request
timeout and bounded retries, and on every exit path sends the recipe's exit
sub-function (`10 81`), closes its bus and releases the session. Its frames are
kept out of raw telemetry, judged by each frame's own acquisition time against
the hold window, so a capture tap that drains its queue late still drops them
(`frames_excluded` is counted at release; frames a late tap drops afterwards
are not in it). The control-event log gets `rom_backup.start` and
`rom_backup.end` (with the summary) on every exit path. A daemon shutdown or
restart mid-read cancels the job: it sends the exit sequence, releases the
ECM and records the end event (`reason` "cancelled: the daemon is shutting
down"). `scripts/restart_canary.sh` refuses while a backup holds the ECM
(`rom_backup` in `/status.passive.ecm_session_claims`), as it does for a
capture (and so does `canary-update`, exit 3); `--force` ends it and says so.
A stop during the post-read wait for polling to resume ends that wait at once
(`polling_resume_note` "not checked: the daemon is shutting down") and still
records the end event. While the backup holds the ECM, K-line reads of the ECM
wait and a K-line diagnostic function on the ECM is refused ("the ECU backup
holds the ECM session"); other K-line modules (ABS, SRS) share the wire, not
the ECM's session, and are unaffected. The image must pass the checksums, carry
the ECM-reported calibration ID and match a 64-block re-read; the result goes
into the library as `source: ecu_read` with `read_meta`:

```json
{"started_at": "...", "ended_at": "...", "duration_s": ..., "requests": ...,
 "retries": 0, "timeouts": 0, "send_errors": 0, "negative_responses": {}, "frames_excluded": ...,
 "blocks": 16645, "block_size": 63, "range": ["0x000000", "0x0FFFFF"],
 "request_id": "0x7E0", "response_id": "0x7E8", "recipe": "rom_backup.yaml",
 "verified": true, "unverified_reason": null,
 "integrity": {"checksum": {...}, "calibration_id": {"image": "1ZS44A00000",
               "ecu": "1ZS44A00000", "ok": true}, "reread": {"blocks": 64, "mismatches": []}},
 "revision_match": {"revision": "ZS44A-20260920", "status": "matched", "reference": null}}
```

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

## Vehicle profile editing

The dashboard's Profiles page edits the registry (operator request
2026-10-10). Every write lands in the machine overlay
(`profiles.registry_overlay_path`, under `daemon.state_dir`); the tracked
`profiles.yaml` is never written. Each successful edit, revert and VIN read is
audit-logged (`profile.update`, `profile.revert`, `vin.read`).

**`PATCH /control/profiles/{slug}`** takes only the fields being edited:
`name`, `make`, `model`, `year` (`null` clears it), `vin`, and `parameters`, a
partial mapping merged over the profile's current parameters and validated as a
whole `VehicleParameters`. Refusals name the field:

- 422 an out-of-range or unknown parameter (`parameters.final_drive: Input should
  be greater than 0`), a malformed VIN, or a North American VIN whose check digit
  (position 9) is wrong. A non-North-American VIN with a mismatching check digit
  is accepted with a `warnings` entry.
- 422 **evidence required**: changing `mass_kg` needs a new `mass_evidence`;
  changing `final_drive`, `gearbox_ratios` or `tyre_loaded_circumference_m`
  needs a new `driveline_evidence` (the same text again is not new evidence).
- 409 a VIN another profile already holds, or a VIN edit that moves the
  **active** profile off the VIN the startup probe read off the connected
  vehicle (the next start would find no profile for that vehicle and register a
  new, empty one).

The overlay stores parameters field by field. A fact and its evidence form one
unit (`mass_kg` + `mass_evidence`; the three driveline facts +
`driveline_evidence`), stored, merged and reverted whole, so a later laptop edit
to any *other* parameter still reaches this machine, and evidence never pairs
with a number from the other side of the merge.

**When an edit applies.** The response's `applies` says it: the daemon copies
the active profile into its configuration at startup, and every session
manifest records that copy. An edit to the active profile reaches the derived
fuel/trip/power channels, the gear estimate, the dyno, tyre wear and the next
session's manifest only after a restart (`scripts/restart_canary.sh`). The
daemon never restarts itself, and a running session's manifest is never
rewritten. Until then `pending_restart` lists the fields that differ, and
`in_use` shows the values the daemon runs; offline tools that resolve the
profile themselves (MCP analysis tools, scripts) already read the new values,
so the two can differ, visibly, until the restart. `fuel_plant.yaml` and the
engine simulator carry their own displacement in the profile directory and do
not read `parameters.displacement_l`.

**`POST /control/profiles/{slug}/revert`** — `{fields?: [...]}`: `name`,
`make`, `model`, `year`, `parameters.<name>` (reverts its whole evidence unit).
Omitted reverts every override and keeps the VIN, calibration ID and CVN, which
this machine read off the vehicle and cannot be reverted. 409 for a profile the
tracked registry does not hold.

**`POST /control/vin/read`** returns `{"vin_read": {ok, vin, refusal, detail,
raw, negative_response_code, check?, held_by?}}`. The request goes out on the
OBD collector's own bus handle between its polls, in a bus-budget slot of its
own (never a second handle beside it, and never inside a Mode 22 window). Refusals before anything is sent:
`unsupported` (`obd_source` is not `canable`), `passive_armed`, `ecm_held`
(another tool holds the ECM: `kline`, `foreign`, `rom_backup`,
`memory_verify`), `capture_paused` (the collector is not polling), `replay`,
`key_off` (`vehicle_state` `parked`, `engine_off`,
`no_data` or `bench`). After sending: `no_answer`, `refused` (NRC), or
`implausible` (not 17 characters); `not_polling` when the collector did not
take the request within 5 s. `check` is the check-digit verdict; `held_by` names
a profile that already has this VIN. The VIN is never saved by this call.

## Passive mode

CANary's broadcast capture path is a pure sniffer, but the OBD side actively
transmits tester requests (Mode 01/03/06/07/09/
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
`engine_off`/`parked`/`no_data` and behavior models stop scoring, because nothing is
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

`POST /control/obd_request` sends one diagnostic request and returns
whatever comes back, instead of waiting for the poll scheduler's cadence —
useful for probing a specific Mode 06 MID or Mode 22 DID, or reading a Mode
09 InfoType (VIN, calibration ID, CVN), without arming a capture or writing
a one-off script. `service` is not filtered: any service byte is built and
sent, write and actuation services included.

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
    "raw_hex": "...", "decoded": {"ascii": "..."},
    "negative_response_code": null, "negative_response": null
  }
}
```

`responded: false` means no answer arrived within `timeout_seconds`
(default 2 s, capped at 10 s) — silence, which on the wire is the same as
"this ECU doesn't support it." A **refusal is not silence**: when the ECU
answers `7F <service> <nrc>`, `responded` is `true`, `raw_hex` holds that
reply, and `negative_response_code` / `negative_response` name the code
(e.g. `0x80` `serviceNotSupportedInActiveDiagnosticSession` — what `21 10`
draws outside session 0xC0 on the Xterra ECM). The two demand opposite next
steps, so they are reported differently (since 2026-09-22). `decoded` is
populated when a Mode 01/02 PID, Mode 03/07/0A DTC list, Mode 09 ASCII
payload, or catalogued (`data/dids.yaml`) Mode 22 DID matches; otherwise only
`raw_hex` is set. MCP equivalent: `obd_request(service, identifier, header,
timeout_seconds)`.

This transmits like any other OBD request, so the same [passive
mode](#passive-mode) caveat applies: arm it first if another tool currently
owns a diagnostic session with the target module.

## Diagnostic functions

One-shot routines a scan tool runs on a module, done by CANary itself and
shown on the dashboard's **Diagnose → Diagnostic Functions** page. Today there
are two: reading a K-line module's stored codes and clearing them. Each is
offered only for a module whose vehicle profile (`daemon.kline_profile`)
records the requests a scan tool was seen to send
(`modules.<addr>.read_codes`, `modules.<addr>.clear_codes`). On the Xterra
that is the airbag module (`SRS`, `0x58`): its clear was sniffed on 2026-10-04
(`data/analyses/2026-10-04-srs-kline-scan-tool.md`) and its code read on
2026-10-08 (`data/analyses/2026-10-08-srs-kline-code-read.md`).

```bash
curl -s http://canary.local:8000/control/diagnostics/functions \
  -H "Authorization: Bearer $CANARY_CONTROL_TOKEN"
curl -sX POST http://canary.local:8000/control/diagnostics/read_codes \
  -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' -d '{"module": "SRS"}'
curl -sX POST http://canary.local:8000/control/diagnostics/clear_codes \
  -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' -d '{"module": "SRS"}'
```

`GET /control/diagnostics/functions` lists one entry per function and module:
`{kind, transport, module, address, request_hex, evidence}`, and for a code
read also `requests: [{request_hex, status, status_confidence}]`.

The K-line collector owns the serial port, so it runs the function: it stops
sniffing (or ends a capture profile's K-line read), wakes the line, sends
StartCommunication, the recorded requests in order (waiting through
`7F xx 78` responsePending) and StopCommunication, then resumes. StopCommunication
is sent after a module's refusal too.

**Read codes.** The reply is

```json
{"read_codes": {"kind": "read_codes", "module": "SRS", "address": "0x58", "ok": true,
  "codes": [{"code": "B1023", "word": "0x9023", "status": "past",
             "status_confidence": "inferred"}],
  "raw": [{"request_hex": "a2 00", "reply_hex": "e2 00"},
          {"request_hex": "a2 01", "reply_hex": "e2 01 90 23"}],
  "reply_hex": "e2 01 90 23", "detail": "read current, past: 1 code(s) stored",
  "started_at": 0.0, "finished_at": 0.4, "duration_s": 0.4}}
```

`codes: []` with `ok: true` is a read that succeeded and found none
(`detail` says "no codes stored"). A failure is `ok: false` with `codes: null`
and the reason in `detail`: the module's refusal (`SRS refused a2 01: …`), a
malformed reply (`SRS sent a malformed code reply to a2 01: …`), or a K-line
failure. Each code's `status` comes from which request returned it, as the
profile records; `status_confidence: inferred` means that mapping rests on a
scan tool's display, not a confirmed answer. The decode is the profile's
`reply_format` (`count_then_words`: `<n> [hi lo]×n`) and `code_word`
(`sae_j2012`: top two bits the letter P/C/B/U, then four hex digits), checked
against the profile's `known_answer` when it loads. `raw` lists each request
and its final reply (responsePending frames are not listed).

**Clear codes.** The reply is
`{"clear_codes": {module, address, ok, reply_hex, raw, detail, duration_s, ...}}`;
a module's refusal is `ok: false` with its reason. A clear only erases stored
codes. A fault that is still present sets its code again.

Refusals before anything is sent, for both: 404 for an unknown module, 409 for
a module with no recorded request of that kind, while passive mode is armed or
another tester is on the K-line, or while another function is running, and
503 when no K-line collector is running. 504 if the collector did not finish
in 20 s.

### Wake-ups (`kline:BREAK`)

Every K-line wake-up is stored as a `kline:BREAK` row (H05.13), value low +
high in ms when known, so a scan tool's wake-up and CANary's can be compared in
one session (`tools/kline_exchange_report.py` lists them under **Wake-ups**,
each with the StartCommunication it led to and whether the module answered).

- `origin: foreign`: a break the sniffer heard. The sniff port runs in
  mark-parity mode (`PARMRK`, set and read back by `KlineTransport.open`), so
  the kernel reports a break as `FF 00 00`. The UART reports a break about one
  character (~1 ms at 10400 baud) after the line falls and never reports its
  release, so `low_ms` and `high_ms` are `null`; `low_ms_min`/`low_ms_max`
  bound the low, and `span_ms` (the value) runs from the falling edge to the
  next byte's start bit, within `timing_uncertainty_s` (read-time bounds plus
  the UART's 32-bit receive timeout). `detected_ns`, `prev_activity_ns` and
  `next_byte_ns` are the host's monotonic clock.
- `origin: canary`: a wake-up CANary drove. `low_ms` and `high_ms` are
  host-clock readings around the break ioctls (pin latency unmeasured, so
  `timing_uncertainty_s` is `null`), with `wakeup` (the profile's name),
  `module`, `answered` and `readback` (what the port read of its own pulse).

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
narrow by construction: only Mode 22 reads and, when `session` resolves to
something, Mode 0x10 session control are ever sent by this job — it builds no
other service, whatever `obd_request` may allow elsewhere.
`session="auto"` (the default) resolves the gating sub-function from
`data/dids.yaml`'s access block, matching what the live scheduler's DID
windows already use for this vehicle.

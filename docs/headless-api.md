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
| `GET /identity` | who this daemon is, for discovery on the network (no token, no state): `product` (`canary`), `api` (`identity/1`), `hostname`, `host_kind` (`raspberry_pi` from the device tree, else `host`), `profile` (the active vehicle slug or `null`), `version`, `git_sha`, `dashboard_port`. A laptop in companion mode sweeps a LAN with this (`src/companion/discovery.py`) |
| `GET /companion/status` | companion mode only (`companion.enabled`; 404 otherwise): the library (`library_root`, `sessions`, `models`, `datasets`, `queued_actions`), the linked Pi (`pi`: `{base_url, identity, source, selected_at}` or `null`; `source` is `discovered` / `selected` / `configured`), `pi_token_present`, and `discovery` (`state` `idle`/`running`/`done`, `ran_at`, `found[]`, `error`). See "Companion mode" below |
| `GET /status` | storage path/free space, latest session, loaded model versions, vehicle, anomaly counters; running calibration (`ecu_calibration_id`, `ecu_calibration_read_at`, `calibration_matches_registry` true/false/null, `calibration_alert`) and `tuning_readiness` chips `{ok, value, detail, fix}` (W02); `startup` — when this daemon started recording (S01.1, below); `network` — the joined SSID, addresses and `dashboard_url` by IP (P2.3; a cached read up to 30 s old with its `age_s`, `null` until the first read, never waits on nmcli). `?view=compact` is the pollable projection and carries `startup` too |
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

The dashboard accepts the token in the URL fragment so the desk launcher
(`scripts/canary_desk.py`) can open it already authenticated:
`http://127.0.0.1:<port>/#wb/<item>?token=<percent-encoded token>`. A fragment
is never sent to the server, and `src/lib/tokenHandoff.ts` moves the token into
the browser's storage and rewrites the address to `#wb/<item>` before the app
renders.

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
| `GET /control/profiles` / `POST /control/profiles/{slug}/name` | list vehicle profiles / set a profile display name. The listing also carries each profile's `tracked` flag and `overrides` (fields this machine's overlay sets, `parameters.<name>` for parameters), `in_use` (the `vehicle:` block the daemon is running, copied at startup), `pending_restart` (active-profile fields whose registry value the daemon does not run yet), `resolution` (how this start chose its profile: `how`, `vin_read`) and `parameter_fields` (label, unit, kind, evidence field per vehicle parameter), plus per profile `delete_refusal` (why Delete would refuse now, else `null`) and a top-level `deleted` list (tracked profiles this machine hid) — see [Vehicle profile editing](#vehicle-profile-editing) |
| `PATCH /control/profiles/{slug}` / `POST /control/profiles/{slug}/revert` | edit a profile's `name`, `make`, `model`, `year`, `vin` and partial `parameters` / put `fields` (or every override) back to the tracked registry's values — see [Vehicle profile editing](#vehicle-profile-editing) |
| `DELETE /control/profiles/{slug}` / `POST /control/profiles/{slug}/restore` | remove a profile from this machine's registry (a tracked one is hidden here by a tombstone and stays in git; recorded data is kept and `data_left` names where) / bring a hidden tracked profile back — see [Vehicle profile editing](#vehicle-profile-editing) |
| `POST /control/vin/read` | read the connected vehicle's VIN (Mode 09 0x02) through the running OBD collector; returned for confirmation, never saved — see [Vehicle profile editing](#vehicle-profile-editing) |
| `GET /control/power` / `POST /control/power/keepalive` | UPS watchdog status / set or cancel the keep-alive hold — see [Power / UPS status](#power--ups-status) |
| `GET /control/update` | software update: `installed` (`version`, `sha`), `status` (the root runner's `/run/canary/update_status`: `state` `idle`/`checking`/`updating`/`done`/`failed`/`refused`, or `lost` when its heartbeat is over 3 min old; `phase`, `phases`, `before`/`after`/`target`, `exit_code` with `exit_meaning`, `outcome`, `message`, `summary`, `log_tail`), `last_check` (`current`, `upstream`, `behind`, `commits` with subjects, `dirty`, or `ok: false` with `reason` `no_internet`/`remote_refused`/`no_upstream`), `pending_request`, `trigger_installed`, `internet.default_route` and `blocked_by` (what an update would end) — see [docs/pi-setup.md](pi-setup.md#updating-from-the-dashboard) |
| `POST /control/update` | `{action: "check"\|"update", full?}` files a request for the root `canary-update.path` (202). 409 before filing when a capture is recording or an ECU backup runs (update only), when a check or update is already running or waiting, or when the Pi has no default route (`no internet: …`); 503 when `canary-update.path` is not installed or `/run/canary` is not writable |
| `POST /pair/request` / `GET` / `DELETE /pair/request/{id}` | **no token**: ask to pair this device (`{name}` → `{id, secret}`), poll it with header `X-Pairing-Secret` (`status` `waiting` with `step` `set_acc`/`turn_on`/`back_to_acc`/`unavailable`, then `approved` with this device's `token` exactly once, or `conflict`/`expired`), or cancel it. The person in the car approves by turning the key ACC → ON → ACC — see [docs/ble-provisioning.md](ble-provisioning.md#pairing-by-key-cycle) |
| `GET /control/devices` / `DELETE /control/devices/{id}` | list paired devices (name, method, paired/last-seen times; never the token) / remove one, which revokes its token |
| `GET /control/network` | what the Pi joined (P2.3): `network` `{hostname, mode, ssid, wifi_interface, addresses, dashboard_url, read_at, age_s, error}` — the BLE status characteristic's facts, the URL by IP because Android does not resolve `canary.local` — plus `provisioning` `{window_remaining_s, max_minutes}`. Read via `nmcli`/`ip` and cached 5 s. Without NetworkManager (a laptop) `mode` is `unknown` and `error` names the missing tool; never a 500 |
| `PATCH /control/devices/{id}` | rename a paired device (`{name}`, 1–128 chars, cleaned to 64 printable); saved before it shows; 404 for an unknown id, 503 when the disk refuses |
| `GET /control/devices/me` | who this token is: `{kind: "device"\|"shared-token"\|"loopback", shared_token, device}` — the dashboard marks "this device" with it |
| `GET /control/pairing/requests` | requests waiting to pair, oldest first: `{id, name, age_s, expires_in_s}`, never their secrets |
| `POST /control/pairing/requests/{id}/approve` | approve that one waiting request on the caller's authority (a paired device or the shared token), bypassing the key cycle and its two-at-once rule; the device's `method` becomes `approved-by:<device id>` or `approved-by:shared-token`. 404 unknown, 409 no longer waiting (approved, conflict, expired). Publishes `pairing.approved` on `/ws/control`, as a key-cycle approval does; `pairing.requested` announces each new request. `canaryctl pair` wraps it, and on the Pi `canary-token pair` runs that against the local daemon with the token file |
| `GET /control/network/provisioning` / `POST /control/network/provisioning/window` | BLE provisioning window: seconds remaining / open it for `{minutes}` (at most 30, `0` closes). While it is open a phone may give the Pi new WiFi credentials over Bluetooth — see [docs/ble-provisioning.md](ble-provisioning.md) |
| `GET /control/network/wifi` | the Pi's saved WiFi networks beside the last scan (N5): `{wifi: {networks, scan, scanned_at, interface}}`. `networks` is every WiFi *client* profile NetworkManager holds, ours (`managed_by: "canary"`, the `canary-wifi-*` profiles) and not (`"other"`, e.g. the Imager's `preconfigured`), never an access-point profile, sorted by `priority` descending then `ssid`: `{id` (the connection UUID, what the other routes take)`, ssid, priority, autoconnect, hidden, security` (`wpa-psk`, `sae`, `wpa-eap`…; `null` is open)`, active, in_range, signal` (0-100, `null` out of range)`, managed_by}`. `scan` is one entry per heard SSID, strongest first, hidden names dropped: `{ssid, signal, security` (nmcli's SECURITY column; `""` is open)`, saved}`. `scanned_at` is the epoch of the last rescan the service made, `null` when it has made none and `scan` is NetworkManager's cache of unknown age; `interface` is the WiFi device, `null` when the Pi has none. Never a secret. The daemon runs unprivileged, so every `/control/network/wifi` route asks the root `canary-provision` service over `/run/canary/network.sock` (`root:canary 0660`; `$CANARY_NETWORK_SOCKET` overrides); when it is not reachable the answer is a **503** `WiFi control service not reachable at <socket>: <reason> (is canary-provision running?)`, never an empty list, and a **504** when it does not answer in time (10 s; 30 s for a rescan; 60 s for a join) |
| `POST /control/network/wifi/scan` | rescan (the radio is busy for a few seconds), then the same `{wifi}`. NetworkManager refuses a second scan within seconds of the last; the cache comes back and `scanned_at` does not move |
| `POST /control/network/wifi` | save a network: `{ssid, password?, hidden?: false, join}`; `join` is required. `join: true` writes the profile, joins it and **undoes it if the join fails** (wrong passphrase, out of range); `join: false` ("save for later") writes and loads it without activating anything, for a phone hotspot or a network somewhere else. `password` is `null`/absent/`""` for an open network. With the network out of range the security type is taken from the passphrase (none = open, else WPA-PSK); in range, from the scan. Returns `{network, wifi}`. **409** `{detail}` when a join fails (the profile is rolled back; `detail` is NetworkManager's reason) or when the SSID is already held by a profile CANary did not write (forget it first; a CANary-written one is replaced); **422** for a passphrase that is not 8–63 printable ASCII characters or 64 hex digits, an empty or over-32-byte SSID, or a malformed body (the error never repeats the body, so never the passphrase). A WPA3-only network saved while out of range is written as WPA-PSK, since nothing has said otherwise |
| `POST /control/network/wifi/{id}/connect` | bring a saved network up now (up to 30 s); `{wifi}`. 404 unknown id, 409 not a WiFi client profile or the activation failed (`detail`). The join may drop the connection the browser is using |
| `PUT /control/network/wifi/order` | `{ids}` in priority order, first wins: NetworkManager's `autoconnect-priority` is set to 100, 90, 80… (steps shrink past ten networks, never below 1). `ids` must be exactly the saved set: 409 `{detail}` naming the missing, unknown or repeated ids otherwise (the list changed under the page, so reload it); `{wifi}` |
| `DELETE /control/network/wifi/{id}` | forget a saved network; `{wifi}`. 404 unknown id, 409 when the id is not a WiFi client profile (the wired profile, an access-point profile and anything else are refused by what NetworkManager says they *are*, not by name) |
| `network.wifi_changed` on `/ws/control` | every `add`, `connect`, `forget` and `order`, done or refused, and the control ledger entry it leaves: `{op, ssid` (add) or `id`/`ids`, `ok`, `join`/`hidden` (add)`}`, plus `error` and `detail` when it failed (`result` is `ok` or `failed`). Never a passphrase |
| `GET /control/passive` / `POST /control/passive` | inspect / arm the CAN transmit gate (receive-only mode) — see [Passive mode](#passive-mode) |
| `POST /control/trigger_poll` | force specific channels due on the OBD scheduler's next tick — `{channels}` — instead of waiting out their tier cadence |
| `POST /control/can_activity` | capture N seconds (capped at 30) of raw CAN traffic — `{seconds, ids?}` — per-id frame rate + which byte positions changed |
| `GET /control/obd_discovery` / `POST /control/obd_discovery/reset` | cached OBD support-discovery state per vehicle / invalidate it so the collector re-walks the Mode 01 / Mode 06 support groups on its next budget slots — see [docs/diagnostic-discovery.md](diagnostic-discovery.md); live progress is `/status.obd_scheduler.discovery` |
| `GET /control/capture/profiles` | list strict, vehicle-scoped capture profiles; `mode22_rate_weights` maps selected DID names to relative cadence (missing members default to 1); `memory_rate_weights` does the same for selected memory channels, and a memory window (one 0x23 read) runs at the largest weight among its selected members |
| `PUT /control/capture/profiles` / `DELETE /control/capture/profiles/{name}` | save or delete a capture profile (`default` cannot be deleted); DID weights must be finite, positive, and name selected `mode22` members. Memory weights are refused (422) unless each is finite and positive and names a channel selected in `memory:`; a key naming a memory *window* is refused with its member channels listed, because a window's rate comes from its members; a selected channel the catalog does not hold is refused (409) as `memory:` always was |
| `POST /control/capture` / `DELETE /control/capture` | start a named isolated capture / return to default collection; start returns capture session path and budget state, stop returns `ended_capture_dir` and restores the exact pre-capture budget. Retrying an identical active start or immediately repeated stop returns the existing result with `idempotent: true`; that stop-response cache does not survive a daemon restart. |
| `POST /control/rom/inspect` | authenticated, read-only ROM inspection: raw `application/octet-stream`, maximum 2 MiB. Decodes in memory, validates source hash/CRCs/table evidence, returns maps, axes, verified live-channel providers, each available table's `trace` (its X/Y trace channels, `axis_mapping` state + evidence, and the scheduler `request_channel` carrying each) and explicit TBD entries. Does not persist the file, change the active tune, or communicate with the ECU. The dashboard no longer calls it: the ROM Editor (which absorbed the ROM Maps page, 2026-10-26) works on library images. The route stays for scripted inspection of an `.OsirisROM` without storing it. |
| `GET /control/ecu/application-model` | authenticated, read-only coverage of the ECM application twin (E05.1), from the profile's `ecu_application_model.yaml`: `model` (name, schema/model version, file sha256), `program` (identity hash and spans, map-slot marker, listed images), `image` (the revision recorded as running now: `known`, `revision`, `role`), the fuel-task `schedule`, and `outputs[]`. Each output has `role` (`final`, or `intermediate` with the output it `feeds`: E05.4's timing-trim lookup is an intermediate of `ignition_timing`, never timing itself) and `status` (`qualified` only with no blockers and an observation channel; `unobserved` with no blockers but no channel; else `conditional`), its `path` of nodes (each with any E05.3 `reference_model` and its `reference_validation`) and the same nodes `by_role` (`implemented` / `observed` / `assumed` / `not_modelled` / `calibration_dead`), `blockers` with their nodes, `equals_output_when`, `regions` (each naming the command `overrides` it holds out), and the ids of the `overrides` acting on it. Also `uncovered[]`, the logic the trace reached but did not resolve, and `overrides[]` (E05.3): each warm-up, closed-loop, transient, fuel-cut or start override with its `regime`, `routine`/`span`, `condition`, `effect`, `established_by` (`listing` / `decompile_text` / `inferred`), `qualification` (`traced` / `fixture` / `vehicle`) with `qualified_by`, `consistent_with`, `gaps` and `verify_with`. 404 when the vehicle declares no model. Does not communicate with the ECU. |
| `GET /control/brain/status` | the second brain's standing (E04.6): `identity` (program id/status, calibration, image), `change`/`last_change` against what it last recorded, `program_specific_state` (invalid after a program change or an unverified program), `predictions` (re-based on a calibration change), `tables` (the twin's table source now), `ram_addresses` (`reverify` only on a program change; a calibration flash leaves them `unchanged`), `transient` (always `unknown` after a restart), `memory` counts, `jobs` counts, `live_twin` (the tick's twin block, E04.1) and `live_proposals` (the tick's next-measurement proposals, E03.4: `open_capture` / `collecting` / `defer` delivered to the page, `review` after the drive; never a driver prompt, never while a capture runs; the same block as `/status` `detectors.inference.proposals`). Read-only: never records a change. MCP `get_brain_status` |
| `GET /control/brain/memory` | `MemoryQuery` over the vehicle's investigation memory: `kind` (comma list), `incident`, `subject`, `session`, `regime`, `calibration`/`program_id` (`unknown` = recorded unknown), `valid_at`/`valid_before`/`valid_after`/`as_of` (ISO with offset; 400 without), `text`, `history`, `limit`. MCP `query_memory` |
| `GET /control/brain/evidence/{id}` | one evidence id resolved (404 unknown, 400 wrong kind), with `part`: `ecu_computation` (the twin) or `engine_response`, and its recurrence count. MCP `resolve_evidence` |
| `GET /control/brain/investigations/{id}/card?as_of=` | the deterministic E01.5 investigation card plus `parts`, its evidence ids split into ECU computation and measured engine response. MCP `get_investigation_card` |
| `GET /control/brain/twin/sessions/{session}` | the target-phi twin's evaluation of a stored session: the job, per-slot `evaluations` (accounting, `missing_inputs`, `capture_requirement`, `trace` of observed/predicted phi with each sample's state), refusals, and `currency` for the program in force. `state: not_evaluated` before a job ran. MCP `get_twin_session_evaluation` |
| `GET /control/brain/jobs` / `GET /control/brain/jobs/{id}` | idle-time brain jobs with `state` (`queued`/`running`/`deferred`/`pending`/`completed`/`failed`) and its named `reason`; one job with its full result. MCP `list_brain_jobs` / `get_brain_job` |
| `POST /control/brain/jobs` | queue `{kind: twin_session_evaluation \| memory_consolidation, params, rerun?}` (202); the job id is the kind plus canonical params, so the same work is one job. Runs in the digest's bounded child only while collection is inactive (`vehicle_state` `parked` or `no_data`, no capture, no replay, no digest or workbench child), yields when collection resumes; an open session stays `pending` until its trip-end digest or a newer session exists. The daemon also queues one itself after each trip-end digest when the session's capture manifest recorded the twin's command word. While a brain child is alive the workbench routes answer 409 `analysis_busy` / `brain_child_yielding` and the child yields. 404 unknown session, 400 no twin definition |
| `POST /control/rom/library?filename=<name>` | ROM library upload (W08.3): raw `application/octet-stream`, 2 MiB cap (413). Accepts a plain image of the vehicle's exact size (1,048,576 bytes on the Xterra) or an `.OsirisROM`; UpRev's backup `.bin` and anything else are refused by name (422). Stored per vehicle under `daemon.rom_library_dir`, content-addressed by the plain image's sha256; the same image again keeps one entry and adds the source. Returns the entry plus `created`. See [ROM library and ECU backup](#rom-library-and-ecu-backup-w08). |
| `GET /control/rom/library` / `GET /control/rom/library/{sha}` | entries newest first (`{entries, count}`) / the full W08.2 description of one image plus its `entry` |
| `POST /control/rom/library/{sha}/edits/export` | ROM Editor export (W10 Part E): body `{label, edits}` as the preview's. The edits applied with both checksum words recomputed, written and re-verified, stored as a new library entry (`source: "rom_editor_export"`) whose source record carries `export_meta`: `parent_sha256`, `parent`, `spec_sha256`, `spec_yaml`, `rom_map` (`profile`, `file`, `sha256`), `calibration_id`, `checksums` (`parent` and `written` words), `cells`, `image_sha256`. Returns `{entry, created, provenance}`; the image downloads through `GET .../download` like any entry. 400 `export_refused` (`kind`, `reason`, `message`): `calibration_not_mapped` (the map does not describe the image's calibration), `parent_checksum_failed` (the parent's own words are wrong), `table_not_verified` (an edited table is only inferred), and every preview refusal. 404 unknown sha, 409 no map, 422 body. Nothing here flashes. |
| `GET /control/rom/library/{sha}/download` | the plain image, `application/octet-stream`, `Content-Disposition: attachment; filename="<label>-<sha8>.bin"` (no `.OsirisROM` export) |
| `PATCH /control/rom/library/{sha}` / `DELETE /control/rom/library/{sha}` | set the label (`{"label": str \| null}`, the only editable field) / remove the entry |
| `GET /control/rom/library/{sha}/tables` | every mapped table in every map slot: raw cells, decoded cells where a decode is declared, axes, confidence and evidence (W08.4) |
| `GET /control/rom/library/{sha}/trace` | the ROM Editor's live-overlay inputs for one library image (ROM Maps merged into the editor, 2026-10-26), read-only: `{sha256, map_slot, afr_stoich, recorded_revision, matches_recorded_revision, live, warnings, tables[]}`. `map_slot` is the slot the trace definitions are written for (the overlay places nothing in another slot); `matches_recorded_revision` is true only when the flash history's running revision names this image's plain sha256; `live` is the vehicle's verified RPM / BFS / commanded-AFR providers; each `tables[]` entry is a `rom_viewer.yaml` kind with its `trace` (X/Y channels, `axis_mapping`, `axis_units`, `stored_decode`, `request_channel`, `out_of_range`) or `null`. The image's own cells and axes stay on `/tables`; the editor joins the two by `kind` and slot. 404 for an image not in the library, 409 when the vehicle has no `rom_viewer.yaml` |
| `GET /control/rom/compare?a=<sha>&b=<sha>` | changed byte ranges a -> b merged with a 16-byte gap, each labelled with the regions and tables it overlaps; table ranges carry per-cell old/new values, decoded where declared (W08.5) |
| `POST /control/rom/library/{sha}/edits/preview` | ROM Editor preview (W09.2): body `{label, edits}`, `edits` being E08.1 edit dicts on the tables `rom_map.yaml` marks `editable` (the 2008: the fuel chain and timing trims), a slotted copy in slot 1 only. A vehicle without a simulator previews from its map alone; `spec_yaml` is null unless every edited table is verified (`export` says why). Returns the edit spec (JSON, YAML, sha256), the candidate's sha256, its E08.1 provenance (cells old -> new, changed ranges, reach), the edited tables' after values and `levers` (which lever the edit pulls and what blocks a number per output, E09.14b). Runs in the thread pool, never deferred; nothing is stored. 404 unknown sha, 409 no map, 400 `edit_refused`, 422 body. See [ROM Editor](#rom-editor-preview-and-simulate-w09). |
| `POST /control/rom/library/{sha}/simulate` | ROM Editor simulate (W09.2): body is one `SimRequest`, every field required (nothing is defaulted). Runs the edits on a recorded base over a stated rpm x load grid, base vs candidate, paired Monte Carlo draws, aggregated per cell. In the workbench child (job `rom_sim`): `409 analysis_deferred` while a capture records, `409 analysis_busy` while another child runs, `503 analysis_unavailable`; a refusal is `400 sim_refused`; 404 unknown sha, 409 no map, 422 body. Nothing is stored; the candidate never leaves the simulator. See [ROM Editor](#rom-editor-preview-and-simulate-w09). |
| `GET /control/rom/editor/typical-values?sha=<image>` | ROM Editor typical values (W09.11): the declared typical values of the vehicle whose map reads `sha` (the active vehicle without it) for the Simulate form's stated environment and learned state (`data/<vehicle>/sim_typical_values.yaml`), each with its `kind` (`typical`, or `placeholder` for a number the simulator reads nowhere) and its `source`, plus any fillable field it leaves empty with a `reason`. Nothing stored, and it changes no request field: the page only fills its form with these, visibly, when the operator presses *Fill typical values*. 409 `typical_values_undeclared` when the vehicle has no file, 409 `typical_values_invalid` when it fails its schema. See [ROM Editor](#rom-editor-preview-and-simulate-w09). |
| `GET /control/rom/backup` / `POST /control/rom/backup` / `POST /control/rom/backup/cancel` | ECU flash backup job (W08.7), read-only: state + live preconditions / start (202, or 409 naming every refusing precondition) / cancel a running read (409 when none runs) |

The frame, every `/analysis/*` route in this table (W04) and the ROM Editor's `simulate` (W09.2) run in the digest's bounded single-flight child and refuse while a capture is recording: `409 {"error": "analysis_deferred", "profile", "message", "laptop_path"}`. A second, different analysis (or a running digest) is `409 analysis_busy`; a child failure is `503 analysis_unavailable`. Calculated load is refused as an axis.

`GET /control/sessions?captures=true` lists only `_capture-*` sessions and
tags each from its `capture_profile.json` sidecar; the default listing remains
normal drive sessions only.
| `GET /control/diagnostics/functions` / `POST /control/diagnostics/read_codes` / `POST /control/diagnostics/clear_codes` | diagnostic functions the vehicle profile records; read or clear one K-line module's stored codes — `{module}` (see [Diagnostic functions](#diagnostic-functions)) |
| `POST /control/obd_request` | one-shot OBD/UDS request — `{service, identifier?, header?, timeout_seconds?}`; `service` accepts any byte, including state-changing and actuation services. CANary must never use it to write ECU non-volatile memory or reflash a module |
| `GET /control/tuning-mode` / `POST /control/tuning-mode` | inspect / arm the focused high-rate tuning poll profile (narrows Mode 01 to a tuning channel set, suspends DID windows) |
| `GET /control/feedback` / `POST /control/feedback` | read / record anomaly-feedback verdicts |
| `GET /control/training-runs` | training-run ledger |
| `GET /control/vehicle-ledger` / `POST /control/vehicle-ledger` / `DELETE /control/vehicle-ledger/{id}` | vehicle maintenance/intervention ledger. POST body `{occurred_at, kind, title, details?, odometer_km?, affected_channels?, corpus_boundary?, event_id?, tires?, intervention?}`; `intervention` is `hardware` \| `fuel` \| `instrumentation` (E01.4: the change is recorded into investigation memory as an intervention; omitted = unclassified). `event_id` replaces that entry whole, so a correction re-states `intervention` or erases it |
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

## Power / UPS status

`GET /control/power` returns `{power: {...}}` from the `ups-shutdown` watchdog's
heartbeat (`/run/canary/ups_status`; `scripts/ups_shutdown_watch.py`), plus the
keep-alive hold read fresh from its own file. The watchdog reports **conditions,
not one flag** (D03.67). They are independent measurements, so `input_lost:
false` beside a falling or critical SoC is a normal reading, not a contradiction:

| Field | Meaning |
|---|---|
| `watchdog_running`, `watchdog_stale`, `status_age_s` | no heartbeat at all / heartbeat older than 10 s (hung) / its age. Every other field is `null` when not running. |
| `armed` | false when no fuel gauge answers on I2C (bench, no UPS): the watchdog does nothing. |
| `input_lost` | the PLD line only (GPIO6 low). |
| `discharging`, `discharge_rate_pct_per_hour`, `min_discharge_rate_pct_per_hour` | SoC falling faster than the floor (default 6 %/h) over the 120 s window, with the measured slope (negative = discharging, positive = charging). `null` until 40 s of history. True with input present on a sagging supply (key-on, engine off at 11.5 V). |
| `soc_critical`, `critical_soc_pct` | SoC at or below the floor (default 15 %), whether the cell is draining or charging. |
| `shutdown_state` | what the countdown is doing: `disarmed`; `input_present` (nothing counts, whatever the SoC says); `held_external_power` (input lost but the battery is confirmed flat: bench USB-C); `counting_down` (`seconds_lost` toward `grace_s`, then power off); `held_keepalive` (a countdown that would run is overridden by a hold). |
| `countdown_reason` | why it counts (set for `counting_down` and `held_keepalive` only): `input_lost_discharging`, `input_lost_soc_critical`, `input_lost_unconfirmed` (no gauge verdict yet, or `UPS_REQUIRE_DISCHARGE=0`: PLD alone). |
| `on_battery` | **legacy alias**, kept for old readers. True only when `input_lost` and the battery is draining or critical; false when input is present or the battery is confirmed flat; `null` when input is lost and the gauge has no verdict yet. It is never true beside `input_lost: false`. New code reads the fields above. |
| `soc`, `vcell`, `seconds_lost`, `grace_s`, `keepalive_remaining_s` | gauge reading, cell volts, seconds of the running countdown, grace window, hold left. |

**Keep-alive vs critical SoC: the keep-alive wins.** The watchdog's loop checks
the hold before it counts: while a hold is active it resets the countdown, so the
critical floor (`UPS_CRITICAL_SOC`) does not override it, and an armed hold keeps
the Pi up at 5 % SoC with input lost (`shutdown_state: held_keepalive`,
`countdown_reason: input_lost_soc_critical`). Without a hold, input loss at a
critical SoC counts down at once even if the gauge shows no discharge, and with
input present nothing counts at any SoC. The hold expires on its own and the
countdown then starts from zero. Pinned by
`tests/test_ups_shutdown.py::test_keepalive_beats_critical_soc_in_the_main_loop`.

A watchdog older than D03.67 sends none of the new fields: they read `null` and
its own `on_battery` (draining or critically low, input or not) passes through
unchanged until the watchdog is redeployed (`install_pi.sh`, or restart
`ups-shutdown` under sudo on the Pi).

## Companion mode (a laptop linked to a Pi)

With `companion.enabled` in the config (`scripts/canary_desk.py` sets it, with
`--companion off` to leave it out), the daemon keeps a **library**
(`companion.library_root`, default `<state_dir>/library`) whose `sessions/`
is its own session store, and serves these routes beside the dashboard. They
replace the standalone companion API and `scripts/canary_companion.py`
(retired 2026-10-16): one process, one port, one site. Every mutating route
takes the control token like the rest of `/control/*`.

At startup, with `companion.discover` on and no Pi configured
(`companion.pi_host`) or remembered, the daemon searches the network once in
the background: `CANARY_HOST`, an mDNS browse for `_canary._tcp` (the Pi
installer has avahi advertise it; needs the `companion` extra, `zeroconf`, and
a host that receives multicast, which WSL under NAT networking does not), the
conventional names, the wired address `192.168.137.2`, then a sweep of each
attached /24 (`companion.discover_sweep`), each candidate probed with
`GET /identity`. `discovery.mdns` in `GET /companion/status` says whether the
browse is available. A lone daemon found is linked
(`source: discovered`); several are listed for the dashboard to choose from.
The link is remembered in `<library>/pi.json` and wins over `companion.pi_host`.

| Route | What it does |
|---|---|
| `GET /companion/status` | Public; the library counts, the linked `pi` and the last `discovery` (above) |
| `GET /companion/discover?sweep=` | Search again now (a few seconds) and return `found[]` and the `pi` in use |
| `POST /companion/pi` `{base_url}` | Link to one daemon by host or URL; it must answer `GET /identity` (or `/status`) now, else 400 |
| `DELETE /companion/pi` | Unlink (back to `companion.pi_host`, if any) |
| `POST /companion/sync/sessions/{id}` | Pull a session export from the linked Pi into the library, resumable and checksum-verified; `pi_host` / `token` in the body override the link and the token from `companion.pi_token_env` |
| `POST /companion/sessions/import` | Import a session archive (base64) into the library |
| `POST /companion/models/{type}/publish` | Push a local model artifact and manifest to the linked Pi, with a compatibility report and accepted/active confirmation |
| `GET /companion/actions`, `POST /companion/actions/{id}/retry` | The queued/failed round trips and their retry |
| `GET /companion/sync/state` | Source hosts, queued actions, publish history and the `pi` in use |

## ROM library and ECU backup (W08)

Read-only throughout: these routes store and describe flash images and read
the ECU's flash. Nothing writes the ECU. The vehicle's image layout is data
(`<profile>/rom_map.yaml`, validated by `src/analysis/rom_map.py`; every
region and table carries `confidence` = `verified` / `inferred` / `unknown`
and required `evidence`), and so is the read recipe: `<profile>/rom_backup.yaml`,
else the vehicle's platform's (`data/platforms/<id>/rom_backup.yaml`, matched by
the profile's make, model and year; `src/vehicle/platforms.py`). Every
2005-2015 Xterra shares `data/platforms/nissan-xterra-n50/`. A vehicle with no
map can still back up, store, list, describe, compare and download images;
only `GET .../tables` (409) and the map's verdicts need one, and a plain upload
is refused by name (nothing says what size the image is), while an
`.OsirisROM` is accepted. Addresses in payloads are integers (byte offsets into
the plain image).

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
  "map_reason": null, "map_profile": "nissan-xterra-2008",
  "extent": {"verdict": "no_sign", "summary": "nothing points past 0x0FFFFF: ...", "...": "..."},
  "read_meta": null
}
```

`source` is the first source; `sources` every distinct source, oldest first.
`revision_match.status` is `matched` (a flashed revision's `image_sha256` in
`tune_revisions.yaml`), `reference` (a never-flashed known image, e.g. the
UpRev base, named in `reference`), `unrecognised`, or `unknown` when the flash
history could not be read (`reason` says why; the image was never compared). `checksum` is `null`, and
`map_reason` says why, when no map definition covers the image's calibration
ID. `map_profile` names the profile whose map describes the image (`null` when
none does). Which maps are asked is `daemon.rom_map_lookup`: `active_profile`
(a Pi, and the default) asks only the scoped vehicle's; `calibration_id` (the
desk, `scripts/canary_desk.py`, and the default in companion mode) asks every
`data/<slug>/rom_map.yaml` and takes the one listing the image's calibration,
so a desk scoped to the 2008 reads the 2013's ROM with the 2013's map. That
profile's tracked `tune_revisions.yaml` is then the flash history
`revision_match` reads, and the ROM Editor's preview, export, simulate and
typical values all use that profile. Two maps listing one calibration is a 422
naming both files. A failing checksum is reported, never refused. `extent` is the image's own
size check, the backup job's `integrity.extent` (below), computed for every
image; `null` when the vehicle's backup recipe names no vector tables.
`read_meta` is the latest ECU read's metadata (below), `null` for uploads.

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
- `entry`: the library entry above, whose `extent` is the image's own size
  check (the backup job's `integrity.extent`, below; `null` when the vehicle's
  backup recipe names no vector tables).

**Tables** (`GET /control/rom/library/{sha}/tables`): `{sha256, calibration_id,
map_slots: [1, 2, 3, 4, 5], tables: [...], reason}`. Each table:

```json
{"id": "fuel_targets", "kind": "fuel_targets", "name": "Fuel Targets",
 "label": "Fuel Targets (Map 1)", "short_name": null, "group": "fuel",
 "slot": 1, "offset": 918048, "end": 918111,
 "rows": 8, "cols": 8, "dtype": "u8", "role": "table", "editable": true, "unit": "AFR",
 "raw": [[128, 128, ...], ...], "decoded": [[14.7, 14.7, ...], ...],
 "decode": {"formula": "afr = 1881.6 / raw", "kind": "reciprocal", "quantity": "afr",
            "unit": "AFR", "confidence": "verified", "evidence": "..."},
 "axes": {"rows": {"id": "fuel_targets_rpm_axis", "name": "...", "offset": 51877,
                   "raw": [24, 32, ...], "decoded": [1200.0, 1600.0, ...], "unit": "rpm",
                   "decode": {...}, "confidence": "verified"},
          "cols": {"id": "fuel_targets_bfs_axis", "raw": [52, 68, ...], "decoded": null, ...}},
 "confidence": "verified", "evidence": "..."}
```

`group` (`fuel`, `ignition`, `airflow` or `emissions`) and `short_name` are
the ROM Editor's table tree (W10.7). `rom_map.yaml` must give every table that
is not an axis a group. A name over 36 characters needs a short name, which
the tree shows in its place. An axis (`role: "axis"`) has neither.

A table without a declared decode (`K`, `be16` at slot +0x762, `inferred`)
has `decoded: null` and is shown raw; a scalar is `[[value]]`. `role` is
`axis` for a table another table uses as an axis. `slot` is `null` for a
single-copy table.

Each axis also carries `editor_labels`, `null` unless the map declares how a
tuning editor labels it. The Xterra maps declare UpRev's BFS in ms on the
Fuel Targets and timing trim load axes, whose code compares raw X counts:

```json
"editor_labels": {"editor": "UpRev", "quantity": "base fuel schedule", "unit": "ms",
                  "formula": "base fuel schedule = raw / 128 × load_point_scaler",
                  "values": [1.35, 2.70, ..., 21.57], "confidence": "inferred",
                  "evidence": "..."}
```

`values` are computed from the named table's value in the same image, so an
edited Load Point Scaler moves them (`null` when that table has no value).
They are labels only: `decoded` stays `null`, and edits, traces and the
simulator keep the raw counts.

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

`state` is `idle`, `refused` (with `reason`), `running`, `done` (image stored:
`stored: true`, `sha256`, `revision_match`), `failed` (with `reason`) or
`cancelled`. `stored` says whether the image is in the library, and `sha256`
is set only when it is: an image that read completely but failed integrity (a
check ran and disagreed: checksum, calibration ID, re-read) is still stored,
`failed`, marked unverified (`reason` starts `unverified:`, `stored: true`).
A check with nothing to check against (no map describes the image, so no
checksums; with no map at all, nothing confirms the recipe's range is the whole
flash; the ECM never reported its calibration ID) leaves the read `done`
with `verified: false` and the `unverified:` reason. A read that finished but
could not be written is
`failed` with `stored: false`, no `sha256`, and a reason ending "nothing was
kept". `send_errors` counts frames the bus itself refused (`can.CanError`);
they are retried like timeouts and named "CAN send failed", never confused
with the transmit gate suppressing a frame. `integrity` is
`{checksum, calibration_id: {image, ecu, ok, offset}, reread: {blocks, mismatches},
extent}`, or `null` if the checks themselves failed. `offset` is where the ID
sits: the map's, or, with no map that describes the image, where the
ECM-reported ID was found as ASCII.

`extent` is what the image's own program says about its size, read offline
from the bytes (the recipe's `extent` section, `src/analysis/rom_extent.py`;
`null` when the recipe has none): `{verdict, summary, image_size, ram_from,
tables: [{offset, entries, valid, reason, inside, ram, erased, beyond,
beyond_count}], calls: {pairs, inside, ram, beyond, beyond_count,
beyond_pairs} | null}`. `verdict` is `larger` (a valid vector table points
past the image's end and below RAM: the flash is bigger than the recipe
reads), `no_sign` (nothing points past it; a sign, not proof) or `unknown`
(no table could be read). Call targets are evidence only, never the verdict.
The read checks the tables as soon as their bytes are in and stops `failed`
("stopped at block N of M: ...", nothing stored) on `larger`; nothing past
the recipe's range is ever requested. Without a map, `no_sign` still leaves
the size unchecked, and the `unverified:` reason quotes the summary. While not
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
 "request_id": "0x7E0", "response_id": "0x7E8",
 "recipe": "data/platforms/nissan-xterra-n50/rom_backup.yaml (platform Nissan Xterra N50, 2005-2015)",
 "verified": true, "unverified_reason": null,
 "integrity": {"checksum": {...}, "calibration_id": {"image": "1ZS44A00000",
               "ecu": "1ZS44A00000", "ok": true, "offset": "0x008204"},
               "reread": {"blocks": 64, "mismatches": []}},
 "revision_match": {"revision": "ZS44A-20260920", "status": "matched", "reference": null}}
```

## ROM Editor: preview and simulate (W09)

Three routes (two under the ROM library, and the vehicle's typical values), all behind the control token (a request
without one is 401, ahead of field validation; but FastAPI parses the body before it runs
dependencies, so a body that is not JSON at all is a 422 even without a token). They edit
nothing the daemon keeps: no candidate image is stored, added to the library or
offered for download, and nothing is written (a candidate is `flashable: false`
with stale checksums on purpose). What a caller keeps is the **edit spec YAML**
in the preview's `spec_yaml`: reviewable, hashable, and the same input the
simulator takes on the laptop. A flash stays the Flash page's own, explicit,
per-operation act. Both routes need a map for the vehicle (`rom_map.yaml`);
without it they answer `409` with the words of `GET .../tables` (`"ROM map
definition unavailable for this vehicle: ..."`). Simulate also needs the
vehicle's simulator definitions (`409 "ROM Editor definition unavailable for
this vehicle: ..."` without one). A vehicle with no `sim_kit.yaml` at all still
previews, from its map alone (below). An unknown or malformed `{sha}` is `404`, checked before the
map. Every error body below is FastAPI's `{"detail": <value>}`: `detail` is a
string for 404/409-no-map, an object with `error` for the named ones, and
pydantic's list for 422.

**Editable** is the map's word: a table takes an edit when `rom_map.yaml` marks
it `editable: true` (each table's `editable` in `GET .../tables`; an axis
never is). The 2008 marks the fuel chain (`fuel_targets`, `fuel_compensation`,
`k_constant`) and the ignition timing trims (`timing_trim_a`, `timing_trim_b`,
E09.7). **Simulatable** is the simulator's: a slotted copy only in `slot: 1`
(the slot the ECM runs after a cold boot, the one the simulator runs), and a
single-copy table (`slot: null`, the 2013's stock image) has no slot to miss.
A vehicle without `sim_kit.yaml` (the 2013 today) previews from its map alone:
`provenance.effect` and `levers` are `null`, `provenance.ranges` is empty and
`sim_base` is `{"ok": false, "reason": "sim_kit_undeclared", ...}`, so nothing
reads as "nothing blocks". **Exported** needs verified: `spec_yaml` is `null`
when an edited table's `confidence` is not `verified`, and `export` says why
(`{"ok": false, "reason": "table_not_verified", "message": "...", "tables":
["fuel_compensation"]}`; `{"ok": true, "reason": null, "message": null,
"tables": []}` otherwise). Such an edit previews; its spec is never handed out.
A trim edit previews and simulates: the simulate request
states the HI DET latch and knock control (`high_det`, `knock_control`, both
required), and the result's `ignition_timing` block gives the ECU-side change
per trim cell (below; `rom_sim run --high-det --knock-control` gives the same
block). A trim cell is a byte whose bit 7 is a flag the ECM strips (decode mask
`0x7F`): `add_raw` and `scale` act on `raw & 0x7F` and keep bit 7, a result
outside 0..127 is `value_out_of_range`, and a `set_raw` that would change bit 7
is `masked_bits_changed`. A table may carry `raw_range` (`[low, high]`, `null`
when it has none): the raw values the code that reads the cell can take, such as
0..32,767 for a word the ECM compares signed. A value outside it is
`value_out_of_range`, never clamped. The 2008 also marks three main-calibration
words editable, each one cell at an absolute offset with no `slot`:
`first_gear_torque_cap`, `first_gear_ratio_threshold` and
`output_shaft_speed_constant`. No simulator node reads them, so a preview's
levers report `mapped_unmodelled`. Anything else is refused by name: `table_not_editable`,
`slot_not_simulated`. E08.1's refusals keep their names: `no_edits`,
`duplicate_edit`, `no_op_edit`, `value_out_of_range`, `rounding_undeclared`,
`rounding_without_scale`, `selector_ambiguous`, `operation_ambiguous`,
`region_unbounded`, `region_inverted`, `unit_undeclared`, `spec_invalid`, plus
the image's own (`map_does_not_apply`, `wrong_program`, `patch_missing`).

**Edit** (E08.1 `Edit`, validated by `rom_edit_spec.parse_edit_spec`, never a
second schema): `{table, slot, <selector>, <operation>}`, one selector of
`cells: [[row, col], ...]`, `all: true`, or `region: {rpm: [lo, hi], load: [lo,
hi], units: {rpm: "rpm", load: "%"}}`, and one operation of `set_raw: int`,
`add_raw: int`, or `scale: float` with `rounding: "nearest" | "floor" | "ceil"`
(no default rounding). "Set to an engineering value" is inverted to a raw value
by the client and sent as `set_raw`.

### `POST /control/rom/library/{sha}/edits/preview`

Body (`extra` fields are a 422): `{"label": "light load 2 % richer", "edits": [Edit, ...]}`.

`200`:

```json
{
  "tool": "rom_editor", "tool_version": "1",
  "spec": {"schema_version": 1,
           "base": {"revision": "ZS44A-20261004", "file": null, "sha256": null},
           "label": "light load 2 % richer", "edits": [{"table": "fuel_compensation", "slot": 1,
             "cells": null, "all": null, "region": {"rpm": [1500.0, 3000.0], "load": [20.0, 50.0],
             "units": {"rpm": "rpm", "load": "%"}}, "set_raw": null, "add_raw": null,
             "scale": 1.02, "rounding": "nearest"}]},
  "spec_yaml": "# CANary ROM Editor edit spec (E08.1). ...\nschema_version: 1\nbase: ...",
  "export": {"ok": true, "reason": null, "message": null, "tables": []},
  "spec_sha256": "18d1a048...", "candidate_sha256": "dd0c9394...",
  "provenance": {"tool": "rom_edit_spec", "kind": "simulation_candidate", "flashable": false,
                 "route": "edit_spec", "label": "...", "base": {"source": "revision", "revision": "...",
                 "file": "<sha>.bin", "image_sha256": "...", "image_format": "plain", "container_sha256": null},
                 "base_sha256": "...", "candidate_sha256": "...", "spec_sha256": "...",
                 "cells": [{"table": "fuel_compensation", "slot": 1, "row": 3, "col": 3,
                            "address": 919187, "old": 128, "new": 131}],
                 "ranges": [{"start": 919187, "end": 919239, "byte_class": "consumed",
                             "nodes": ["fuel_comp_lookup"], "what": ["fuel_compensation (slot 1)"], "changed": 20}],
                 "effect": {"outcome": "unevaluable", "slot": 1, "slot_source": "default_slot",
                            "changed_bytes": 20, "changed_nodes": ["fuel_comp_lookup"],
                            "moved_lookups": [{"node": "target_afr_lookup", "tables": ["fuel_targets", "..."]}],
                            "rom_effects": {"...": "..."}}},
  "tables": [ <one entry per edited (table, slot), the candidate's values, in the GET .../tables shape:
               id, kind, name, label, slot, offset, end, rows, cols, dtype, role, editable, unit, raw, decoded,
               decode, axes, confidence, evidence, and for fuel_compensation editor_disagreement> ],
  "levers": {"schema_version": 1, "tool": "rom_levers", "tool_version": "1",
             "outcome": "unevaluable", "reason": null, "regime": null,
             "levers": [{"lever": "fuel_compensation", "label": "Fuel Comp", "tables": ["fuel_compensation"],
                         "slots": [1], "byte_class": "consumed", "start": 919187, "end": 919239,
                         "changed_bytes": 20, "changed_cells": 20, "nodes": ["fuel_comp_lookup"], "blockers": []}],
             "outputs": [{"output": "base_fuel_schedule_word", "source": "application_model", "status": "changed",
                          "levers": ["Fuel Comp"], "blockers": [], "assumes_conditioned": ["fuel_comp_load_byte"],
                          "refused_if_conditioned": [], "reached": true, "number": true, "design_dependent": true},
                         "..."],
             "messages": ["Fuel Comp -> base_fuel_schedule_word: a number can be given (for points that state only the fill byte)",
                          "Fuel Comp -> injector_pulse_ms: plant not qualified: ...; closed loop: trim authority undecoded",
                          "..."]},
  "sim_base": {"ok": true, "status": "matched", "revision": "ZS44A-20261004", "reason": null}
}
```

`levers` is `rom_levers.classify_edit(...).to_dict()` (E09.14b) on the comparison the
simulator would use (`compare_roms` under the simulation contract's mode), qualified on
the plant `sim_kit.yaml` declares and with no regime stated (so the closed-loop blocker
is listed). **`levers.messages` is the text to show**: one line per lever and per
reached output, in a stable order, and the very strings
`python -m tools.rom_sim inspect` prints for the same edit (`levers` there is the same
object). Nothing in the daemon, the dashboard or the MCP tool words one of them; a
consumer lists them verbatim. An output reads either `a number can be given (...)` (the
parenthesis names what a design point must state) or what blocks one: `not mapped`
(changed bytes outside `rom_map.yaml`), `plant not qualified: <why>`, `closed loop: trim
authority undecoded`, `unmodelled: <nodes>`, and for the ignition outputs the torque
stage's blockers. While that stage is unbuilt the blocker is `no torque plant`. Since
E07.5 stage 4 (2026-10-17) the stage is built and not validated, so the ignition outputs
read `engine torque not computed in conditional_rom_comparison; torque production built,
not validated (owed H07.10, H07.5, H07.12, H07.8, N01.2 and 6 more); fuel delivery built,
not validated (plant not qualified); spark response unmeasured`. After the per-output
lines, `messages` ends with the edit's own claim, refused by name: `torque/power claim for
this edit (<controls>): refused; <blockers>`. The controls are derived from the comparison
(a timing trim `spark`, Fuel Targets `lambda`, Fuel Comp and K both). So every classified
edit has at least one line, and `messages` is never empty; `the edit reaches no output` is
still the floor for an edit with no output line. The page and the MCP tool read the same
body.

`spec.base` is `{"revision": R, ...}` when the library entry's `revision_match`
is `matched` (the YAML then resolves through `tune_revisions.yaml` on any
machine) and `{"revision": null, "file": "<sha>.bin", "sha256": "<sha>"}`
otherwise. `provenance.cells` are raw old -> new (decode them from `tables`);
`effect.changed_nodes` is the reach and `moved_lookups` the other lookups the
edit moves. `sim_base.ok` is whether a simulate is offered: `false` with
`reason: "base_not_a_recorded_revision"` unless `revision_match.status` is
`matched` (a `reference` image, a tuner's base that was never flashed, is not a
revision); the preview still works.

Errors: `400` `{"detail": {"error": "edit_refused", "kind": <kind>, "reason":
"<name>", "message": "..."}}`; `404`; `409`; `422`. `kind` is the refusal class
(the table of `src.analysis.rom_editor.REFUSALS`): `edit_spec` and `rom_effect`
for an edit or the image (the reasons above, including the image's own
`wrong_program`), and the other classes the code beneath can raise:
`application_model`, `image_format` (reason `image_format`), `fuel_plant`,
`sim_design`, `simulation`. The `reason` list is open-ended; every class in the
table is a 400, never a 500. The preview classifies the edit on the plant the
vehicle's `sim_kit.yaml` declares, so a vehicle without one is refused here as
`simulation` / `sim_kit_undeclared` rather than previewed unclassified.

### `POST /control/rom/library/{sha}/simulate`

Body: one `SimRequest`, **every field required** (a missing one is a 422 naming
it, an unknown one a 422 `extra_forbidden`):

```json
{"label": "K +5 %", "edits": [Edit, ...],
 "grid": "fuel_comp",
 "rpm_edges": [4000.0, 4800.0, 5600.0], "load_edges": [30.0, 60.0, 80.0, 100.0],
 "load_basis": "fill_fraction_as_manifold_ratio",
 "baro_kpa": 80.0, "iat_c": 25.0, "coolant_c": 88.0, "battery_v": 14.0,
 "rail_kpa": "working_assumption", "throttle_pct": 20.0, "pedal_flag": 0,
 "stft_pct": 0.0, "ltft_pct": 0.0, "throttle_closed_learned": 0.0,
 "idle_air_learned": 0.0, "evap_purge_area": 0.0,
 "high_det": "set", "knock_control": "fallback",
 "candidate_events": "none", "draws": 200, "seed": 20261015}
```

`high_det` (`"set"` or `"clear"`) and `knock_control` (`"active"` or
`"fallback"`) are the ignition chain's two ECU states (E09.7), which nothing
logs, so the request states both and a missing one is a 422 naming it.
`high_det` is the knock controller's HI DET latch (0xFFFF283F bit 6): warm,
the ECM reads trim B while it is `set` and trim A while it is `clear`.
`knock_control` `"fallback"` is the knock sensor fault state (0xFFFF479D bit 7,
retard target 0xFFFF13FE = 0xFB00). `"fallback"` with `"clear"` is refused
(`sim_refused`, reason `high_det_forced_by_ks_fault`): in states 2-4 of the
engine state machine 0x027CC4 ([inferred] engine running) the KS fault sets the
latch on every pass (0x04A928; with cal 0x8ADC bit 7 set, pinned), so that
pairing is not a running steady state; the other three are
states. Both change with the truck (the 2026-10-08 RAM read was
`set` / `fallback`, KS1 fault; both clear when KS1 is repaired), so **neither is
ever a typical value**: the dashboard offers no preselection and remembers
nothing, and the typical-values route refuses a file that names either. State
the pairing you are asking about, not the one you last saw. They are read only
by `ignition_timing`; the fuel outputs do not depend on them.

`grid` is `"fuel_comp"` or `"target_afr"` (the aggregation axes). `rpm_edges`
and `load_edges` are the grid's cell edges (strictly rising; `load_edges` in
Fuel Comp's load axis units, % of the fill model's ceiling, within 0-100); one
design point sits at each cell centre. `rail_kpa` is a number or the string
`"working_assumption"` (the plant's own, said so). `candidate_events` is
`"none"` (both ROMs start from the same learned state) or `"ecu_reflash"` (the
flash resets what the contract says it resets on the candidate). `pedal_flag`
is an integer 0-255, `draws` an integer >= 1 and `seed` an integer >= 0, never
defaulted. Bounds, refused before any work: `grid_too_large` (more than
`MAX_GRID_POINTS = 1024` cells) and `draws_too_many` (more than `MAX_DRAWS =
2000`). The base must be a recorded revision (`base_not_a_recorded_revision`).

**Coarse vs fine (W09.9).** Both are the same request with a different `draws`
and different edges, and neither is a server default. For a **quick check**
("did my edit move anything, and where?") send every other Fuel Comp
breakpoint on each axis (about a quarter of the grid points; the page's
*Coarse (quick check)* preset) and `draws: 200`, the count E08 reads cell
quantiles from. For a **thorough run** send the full suggested edges (the
page's *Fine* preset; 224 points on the 20261004 image after cutting the load
edges to 0-100 %) and `draws: 1000`, E08.3's speed target and W09.6's
reference row. Cost grows with points x draws on top of a fixed start-up:
measured on the dev laptop (W09.6, the real `spawn` child, start-up
included), 224 points x 200 draws took 4.5 s, 224 x 1,000 took 8.5 s,
224 x 2,000 11.9 s, 1,024 x 1,000 34.1 s and the cap (1,024 x 2,000) 51.1 s.
A least-squares fit through those seven rows is about `3.3 s + 2.3e-5 s x
points x draws` (off by -20 % to +16 % against them; no run under 224 points
was timed). The Pi is not measured, so treat these as the laptop's, not
yours. Two things differ in the result and an agent must not read a coarse run
as a fine one: table cells with no visited design point come back with status
`no_points`, which is **not** `unchanged` (nothing was simulated there), and
quantiles over few draws are noisy (E08.3: three draws give only a coarse
per-cell spread). `provenance.draws` and `design.points` say what was run.
The dashboard banners any result below the Fine preset's draws or points.
A client that gives up does not stop the server: the job is held behind
`asyncio.shield` and no route cancels it, so a different request is `409
analysis_busy` until it finishes (an identical one re-attaches to it).

`200`: `{"tool": "rom_editor", "tool_version": "1", "label": "simulation",
"synthetic": bool, "caveats": [...] (the result's, first, unchanged),
"assumptions": [...] (plant working assumptions prefixed "plant working assumption: ",
the design's own prefixed "design: ", and others; caveats repeats every one;
the design's are not in design, E08.10), "unresolved": [...], "inert_parameters": [...],
"coverage": {output: share}, "outcome": str, "learned_state": {...},
"provenance": {hashes, plant_version, seed, draws, candidate_events, ...},
"design": {points, weight, base_revision, ..., "grid": {rpm_edges, load_edges,
load_axis, load_unit, load_basis, environment}},
"candidate": <the preview's provenance>, "cells": <CellTable>,
"ignition_timing": <timing block>, "spec_yaml": str | null, "export": {...},
"spec_sha256": str}` (`spec_yaml` and `export` as the preview's). Note `label` here is the result's, the request's is
`candidate.label`. `cells` is `sim_cells.CellTable.to_dict()` (E08.4;
`schema_version: 1`, JSON Schema from `sim_cells.cell_table_schema()`): `grid`,
`table`, `slot`, `rows` / `cols` axis info (`raw`, `breakpoints`, `values`,
`unit`), `reach`, `caveats`, `unresolved`, `status_counts`, `overlay`
(`edited_cells`, `axis_edits`, `other_edits`, `moved_lookups`), `unplaced`,
`headline_by_loop`, the hashes, and `cells`: **every** table cell, row-major
(256 for Fuel Comp), each `{row, col, row_value, col_value, n_points, pairs,
weight, regime_shares, loop_shares, outputs: {name: {status, pairs,
supported_share, unsupported_share, support[], quantiles {over, pairs, weight,
delta {p05, p50, p95}, relative_delta, relative_undefined_pairs} | null,
changed_share, supported_effect}}, by_loop[], edited, row_axis_moved,
col_axis_moved}`. A cell nobody visited has `pairs: 0` and is not
`unchanged`. A 16 x 16 table with six points measured about 370 KB of JSON on
the synthetic image; the form's 224-point grid about 705 KB (W09.6; 757 KB on
the synthetic image), because 224 of the 256 cells are visited and each carries
six outputs. The route's body and the dashboard's payload are always this whole
body; the MCP tool `simulate_rom_edit` cuts it in the tool layer by `detail`
(`headline`, the default, `summary`, `cells` or `full`) and states what it
dropped in the result (docs/mcp-server.md).

**`ignition_timing`** (E09.7) is on every result, from
`sim_cells.ignition_timing_block` (the block `tools/rom_sim.py` prints) and
`ignition_timing_points`. ECU side only: the timing byte's chain with every
term but the selected trim and the base term 0xFFFF27C6 held equal (27C6 is
computed per ROM; see "ranges" below), no torque and no knock claim.
`status` is `evaluated` or `unevaluable`; `state` echoes the stated
`{high_det, knock_control, source}`; `trims_changed` says whether an edit
touched a trim and `base_timing_tables_changed` whether it touched a table or
calibration byte of the 27C6 chain or of 0xFFFF279A's inputs (either one
without a stated state is refused `timing_state_unstated`);
`other_inputs_changed` lists `timing_input_changed:<node>` for every other
changed byte that reaches `ignition_timing` (a K or Fuel Comp edit:
`air_charge_tp`, `fuel_comp_lookup`) and `unknown_bytes_changed` for one no
node reads. Any of those makes the block
`unevaluable` with those `reasons` and no cells (the trims' grid holds that
change's terms equal, so its "0 changed" would be a claim the chain cannot
make). Otherwise `cells_at` names the coolant the cells are read at (the
request's `coolant_c`), `summary` counts `{cells, changed, unevaluable,
changed_trim_not_read, reasons}` and `cells` lists the cells of the trims' own
16 x 16 grid that change or name a reason beyond `clamp_headroom_unknown`
(every other cell is unchanged): `{row, col, rpm_word, x, base, candidate,
delta, delta_min, delta_max, candidate_min, candidate_max, trim_term_delta,
term_27c6_delta, term_279a, charge_word_routes, status, reasons, table_read}`,
`delta` in degrees
(1 trim count = 1 degree), `table_read` `a` / `b` / null (the blend band).
**Ranges (E09.7 part B):** 0xFFFF27C6 reads 0xFFFF1B24, which nothing
computes (0x4000 + 0xFFFF175C + 0xFFFF1736 - 0xFFFF26E8, times 0xFFFF26C0:
1736 reads X, 175C reads BFS, 26C0 follows the Target AFR lookup). It is swept
over 0x4000 x [0.7, 1.3]: held equal on both ROMs when the change moves none of
X, BFS and phi (the trim grid, which holds X equal), else swept on each ROM
independently, with `charge_word_inputs_moved` in `reasons` and the routes in
`charge_word_routes` (`"0xFFFF1736 via X"`, `"0xFFFF175C via BFS"`,
`"0xFFFF26C0 via the Target AFR lookup"`). `charge_word_routes` is `[]` when
1B24 was held equal because no route moved it, and `null` when the routes are
not known or do not apply: no timing state stated, or a pair whose X could not
be computed (`load_input_x_not_computed`).
Where 27C6's change differs across that band `status` is `range`, `delta` and
`candidate` are null, `delta_min` / `delta_max` carry the answer (clamps
applied to both ends when a base is known) and `reasons` include
`charge_word_1b24_unresolved`; an exact answer has `delta_min == delta_max ==
delta`. `term_27c6_delta` is `[min, max]` of 27C6's own change (null when it
cannot be computed: `term_1512_unmodelled`, an image whose cal 0x8538 is not
0). `term_279a` is `zero` (map 0x9B09 and curve 0xCC33 read 0 at the point on
both ROMs), `held_equal`, or `unmodelled` (not provably 0 and the edit moves
BFS or one of its tables: `term_279a_unmodelled`, unevaluable). `summary` also
counts `ranged`, and `changed` includes them. An edit to the trim the stated
latch does not read has `delta: 0` and `trim_a_not_read:high_det_latch_set` /
`trim_b_not_read:high_det_latch_clear` on each edited cell. `changed_trim_not_read`
counts the cells that name a `trim_[ab]_not_read:` reason and only those: a trim
the ECM does read, set below the term's floor, is `trim_term_floor_may_bind` and
`unevaluable`, not "not read". With both trims edited the trim the latch reads
gives `delta` and the other is named in `reasons` beside it, so one cell is both
`changed` and counted in `changed_trim_not_read`. `torque` is
`unsupported` with `spark_response_unmeasured...` and `knock` carries its own
refusals. `points` is one row per design point with its outcomes grouped by
`{status, delta, delta_min, delta_max, trim_term_delta, term_27c6_delta,
term_279a, charge_word_routes, reasons, x_base, x_candidate, draws}`; a K or
Fuel Comp change gives the timing change at each ROM's own X (27C6 computed per
ROM with 1B24 swept independently, `charge_word_inputs_moved`, usually a
`range`; `term_279a_unmodelled` where 279A is not provably 0, e.g. cold), a
Target AFR change that moves phi `enrichment_retard_unmodelled`. The
enrichment retard 0xFFFF174A is also held (`enrichment_retard_unmodelled`) when
X moves at a point where either ROM's gated Target AFR byte 0xFFFF174B is at or
above the threshold (cal 0x8792 with HI DET clear, 0x8793 set; read from each
image), the below-threshold gain (cal 0x8797 / 0x8798) is not 0, or the byte is
not known there: its cap 0xFFFF174C reads X. With the pedal flag set the lookup
reads its last column, so a K edit at WOT moves X and not phi and is held
there. The
`timing_input_changed:<node>` reasons of a change that reaches timing only
through BFS and X (K, Fuel Comp, the Load Point Scaler) are not on the points:
the points compute that route themselves, and name what of it they do not
(`charge_word_inputs_moved`, `term_279a_unmodelled`).

Errors (each a `{"detail": ...}`):

| Status | `detail` | When |
|---|---|---|
| `400` | `{"error": "sim_refused", "kind": <kind>, "reason": "...", "message": "..."}` | the child (or the route, for the base) refused. `kind` is one of `edit_spec`, `rom_effect`, `sim_design`, `simulation`, `fuel_plant`, `application_model`, `image_format`, `request` (`request` only for a `run_job` caller that skipped the route's validation: the route answers a bad body 422 first). `reason` is open-ended. The route's tests produce any edit reason above, `grid_edges_invalid`, `grid_too_large`, `draws_too_many`, `base_not_a_recorded_revision` and `flash_history_unreadable`; the code also names `load_outside_model`, `design_empty`, `uncertainty_missing`, `uncertainty_invalid` and `image_hash_mismatch` / `image_not_found` (the library changed under the request), and a new refusal is a new `reason`, not a new shape |
| `404` | `"no ROM library entry '<sha>'"` | unknown or malformed sha |
| `409` | `"ROM map definition unavailable for this vehicle: ..."` | no map |
| `409` | `"ROM Editor definition unavailable for this vehicle: ..."` | the map is there but another definition is not (application model, simulator contract, plant); the message names the file |
| `409` | `{"error": "analysis_deferred", "reason": "capture_active", "profile", "message", "laptop_path"}` | a capture is recording (`laptop_path` says to download the image and run the spec on the laptop) |
| `409` | `{"error": "analysis_busy", "reason": "child_busy" \| "brain_child_yielding", "message"}` | another analysis, digest or brain child is running; an identical request already in flight is shared instead |
| `422` | pydantic's list | a missing, unknown or mistyped body field |
| `503` | `{"error": "analysis_unavailable", "reason": "child_timed_out" \| "child_failed" \| "child_out_of_memory", "message"}` | the child did not finish (900 s), died, or hit its address-space ceiling |

**Order of checks:** 404 (unknown sha), 409 (no map), then the base: an image
whose `revision_match.status` is not `matched` (a `reference`, an `unrecognised`
image, or `unknown` when the flash history is unreadable, in which case the
message says why) is a 400 `base_not_a_recorded_revision` **before** anything is
deferred or spawned, so it is the same answer whether or not a capture records.
Then the capture deferral (409), then the child (409 busy, 503, 400). The
child reads the flash history itself (tracked file merged with the dashboard's
operational one) only for a matched base; if it has become unreadable since the
route matched the image, the child returns it as data: 400
`flash_history_unreadable`.

A refusal raised inside the child crosses the process boundary as data
(`src.analysis.rom_editor.run_job` returns `{"refused": {...}}`): the
simulator's refusal classes do not survive pickling, and one raised out of a
`spawn` child would arrive as `503 child_failed`.

### `POST /control/rom/library/{sha}/edits/export`

Body: the preview's `{label, edits}`. The edits are applied to the entry's
image from the vehicle's ROM map alone (no simulator is needed), both
checksum words are recomputed, written and re-verified
(`rom_targets.Checksums.write`), and the image is stored as a library entry
with `source: "rom_editor_export"`. Its source record, and the entry, carry
`export_meta`:

```json
{"kind": "rom_editor_export", "label": "...",
 "parent_sha256": "...", "parent": {...},
 "spec_sha256": "...", "spec_yaml": "...",
 "rom_map": {"profile": "nissan-xterra-2008", "file": "rom_map.yaml", "sha256": "..."},
 "calibration_id": "1ZS44A00000",
 "checksums": {"algorithm": "be32_sum_xor",
               "parent": {"sum": "0x...", "xor": "0x..."},
               "written": {"sum": "0x...", "xor": "0x..."}},
 "cells": [...], "image_sha256": "..."}
```

`spec_sha256` hashes `spec_yaml`, the same edit spec the preview offers for
download; `rom_map.sha256` hashes the map file's bytes. Response
`{entry, created, provenance}`; `created` is false when the same image is
already in the library (the record gains this export's source). The image
downloads through `GET .../download` like any entry.

Refusals, in order, as 400 `{"error": "export_refused", kind, reason,
message}`: `calibration_not_mapped` (the image's calibration id is not the
one the map describes), `parent_checksum_failed` (the parent's own stored
words are wrong, so a rewrite would launder an unknown change), then the
spec's own: `table_not_verified` (an edited table is only `inferred`) and
every preview refusal. 404 unknown sha, 409 no map, 422 body. It runs in the
thread pool. Exporting writes nothing to the ECU; a flash stays its own act.

### `GET /control/rom/editor/typical-values`

The vehicle's **typical stated values** for the Simulate form's *Fill typical
values* button (W09.11). They are vehicle facts, so they are data
(`data/<vehicle>/sim_typical_values.yaml`, `rom_editor.typical_values`), never
page or code constants, and each value names where it comes from. `?sha=<image>`
names the vehicle: the profile whose map reads that library image (the one its
simulation runs on; the page always sends it). Without it, the active vehicle.
No body, and nothing is stored.

`200`:

```json
{"profile": "nissan-xterra-2008", "file": "sim_typical_values.yaml",
 "file_sha256": "<sha256 of the file>",
 "values": {"baro_kpa": {"value": 81.0, "kind": "typical", "source": "E07.6's stated point (...)"},
            "pedal_flag": {"value": 0, "kind": "typical", "source": "W09.6's grid environment (...)"},
            "idle_air_learned": {"value": 0.0, "kind": "placeholder", "source": "PLACEHOLDER, not a measurement: ..."},
            "rail_kpa": {"value": 350.0, "kind": "typical", "source": "..."}},
 "left_empty": {}}
```

Only the twelve environment and learned-state fields of the `SimRequest` can
appear (`rom_editor.TYPICAL_FIELDS`: `baro_kpa`, `iat_c`, `coolant_c`,
`battery_v`, `rail_kpa`, `throttle_pct`, `pedal_flag`, `stft_pct`, `ltft_pct`,
`throttle_closed_learned`, `idle_air_learned`, `evap_purge_area`). Every one is
either a `values` entry (a number, or `"working_assumption"` for `rail_kpa`
only; a **required** `kind`, `typical` or `placeholder`; and a **required**
non-blank `source`) or a `left_empty` entry (`{reason}`) that says why no value
is declared (the Xterra's is `{}`: all twelve are declared). A `placeholder` is a
number the simulator reads nowhere today, filled only so the form can run; its
source must say it is one (`a placeholder's source must say it is a placeholder`),
and `tests/test_rom_editor.py` fails the moment any code in `src/` reads the
field. A file where a field is neither a value nor left empty, where a value has
no source or kind or is one the `SimRequest` would refuse, or that names any other
field (the grid, the candidate events, the edges, the draws, the seed and the
label are never typical, and neither are `high_det` and `knock_control`: they
are live ECU states, and a typical value would outlive the fault that set
them) is invalid, so the page can always say what it left
for the operator. **The 23-field `SimRequest` still has no
defaults:** a client that wants these values sends them in the simulate body
itself. The dashboard fills them into the visible form on a click, marks each
`typical: <source>` or `placeholder: <source>` from `kind`, and sends nothing it
did not show.

| Status | `detail` | When |
|---|---|---|
| `401` | | no or wrong control token |
| `409` | `{"error": "typical_values_undeclared", "message": "..."}` | the vehicle's profile has no `sim_typical_values.yaml`: it declares no typical values, and none are assumed |
| `409` | `{"error": "typical_values_invalid", "message": "..."}` | the file fails its schema (the message lists each problem by field) |

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
audit-logged (`profile.update`, `profile.revert`, `profile.delete`,
`profile.restore`, `vin.read`).

**`PATCH /control/profiles/{slug}`** takes only the fields being edited:
`name`, `make`, `model`, `year` (`null` clears it), `vin`, and `parameters`, a
partial mapping merged over the profile's current parameters and validated as a
whole `VehicleParameters`. Refusals name the field:

- 422 an out-of-range or unknown parameter (`parameters.final_drive: Input should
  be greater than 0`), a malformed VIN, or a North American VIN whose check digit
  (position 9) is wrong. A non-North-American VIN with a mismatching check digit
  is accepted with a `warnings` entry.
- 422 **evidence required**: changing `mass_kg` needs a new `mass_evidence`;
  changing `final_drive` or `gearbox_ratios` needs a new `driveline_evidence`
  (the same text again is not new evidence). Tire size
  (`tire_diameter_m`, `tire_loaded_circumference_m`) needs none: a tire swap
  saves on its own.
- 409 a VIN another profile already holds, or a VIN edit that moves the
  **active** profile off the VIN the startup probe read off the connected
  vehicle (the next start would find no profile for that vehicle and register a
  new, empty one).

The overlay stores parameters field by field. A fact and its evidence form one
unit (`mass_kg` + `mass_evidence`; `final_drive` + `gearbox_ratios` +
`driveline_evidence`), stored, merged and reverted whole, so a later laptop edit
to any *other* parameter still reaches this machine, and evidence never pairs
with a number from the other side of the merge.

**When an edit applies.** The response's `applies` says it: the daemon copies
the active profile into its configuration at startup, and every session
manifest records that copy. An edit to the active profile reaches the derived
fuel/trip/power channels, the gear estimate, the dyno, tire wear and the next
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

**`DELETE /control/profiles/{slug}`** removes a profile from this machine's
registry (operator request 2026-10-23: a user who does not own the shipped
vehicle should not have to carry its profile). It returns `{deleted, tracked,
data_left}`. The tracked `profiles.yaml` is never written at runtime, so what
"delete" does depends on where the profile lives:

- **Tracked profile** (`tracked: true`): this machine's edits to it are dropped
  and its slug goes into a `deleted:` list in the overlay (a tombstone). It is
  hidden from `GET /control/profiles` here only; it stays in git, and other
  machines are unaffected. `POST /control/profiles/{slug}/restore` removes the
  tombstone and returns the profile as the tracked file has it (the edits it
  had are gone).
- **Machine-only profile** (`tracked: false`): its overlay row is removed. There
  is nothing to restore.

The tombstone survives every later write to the overlay (rename, edit, revert,
auto-register, the calibration-ID refresh). Profile resolution still sees a
hidden profile, but never runs on one: a start whose VIN read, `--profile`
override or `vehicle:` block resolves to a deleted profile restores it and logs a
warning, rather than registering a duplicate of that vehicle.

**Recorded data is never deleted or moved**: sessions, models, anomalies, jobs,
calibration, the ROM library and the profile's ledgers stay where they are.
`data_left` lists the existing directories that hold them (empty when profiles
are disabled or nothing was recorded), so they can be removed by hand or kept for
an offline refit.

Refusals (409, with the reason; the listing carries the same text as
`delete_refusal`, so the dashboard disables the button with it, and nothing is
written):

- **the active profile**: the daemon is running on it; restart it on another
  profile first.
- **the profile `canary.yaml`'s `vehicle:` block names** (the slug derived from
  its make/model/year): a start that reads no VIN falls back to it and would
  register it again. Change the `vehicle:` block first.

404 for a slug the registry does not show (an already-deleted one included).
`restore` is 404 for a profile that is not currently hidden. A VIN held by a
hidden profile still counts as taken: giving it to another profile is refused
(409) naming the hidden holder.

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

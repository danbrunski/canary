# MCP server (`canary-mcp`)

The CANary MCP server exposes the running daemon to MCP-speaking clients —
Claude Code on a laptop, Qwen-Agent on the Pi, anything that speaks the
[Model Context Protocol](https://modelcontextprotocol.io/).

It's a separate process from `src.daemon`. It calls the dashboard HTTP
control plane on localhost for live state and reads Parquet directly for
ad-hoc SQL. Adding it doesn't touch the capture / inference hot path.

The MCP surface deliberately cannot mint an ECU non-volatile-write
`Authorization`. W07 binds that short-lived, single-use capability to an
exact image hash and ECU through an operator-present confirmation path; an
agent can inspect state and ordinary diagnostics here, but cannot grant itself
permission to program a module.

---

## What's exposed

Read tools:

| Tool | Wraps |
|---|---|
| `get_status` | `GET /status` (incl. derived `vehicle_state.{state, rpm, coolant_temp, run_time, obd_freshness_seconds, can_freshness_seconds, supply_voltage, replay_id}` — check this before concluding anything about detector behavior; states are `engine_running`, `ignition_only`, `engine_off`, `parked` (both buses asleep on a live battery: the HAT's 12 V sense), `bench` (no vehicle on the port: USB-C or UPS power), `no_data`, `replay`, `passive` (CANary not transmitting, so OBD staleness says nothing; `vehicle_state.passive` is also true on any verdict reached under passive), `unknown`. Also `model_provenance` — per-loaded-model `{model_version, created_at, training_data_hash, training_sessions, artifact_format_version}` off each manifest (Task 6.5); `health.can0` — can0's rx overrun/drop/error sysfs counters plus the delta since the daemon started watching, `null`/absent when there's no `can0` interface (Task 6.8, see `scripts/verify_artifacts.py` for the companion deploy-skew check, Task 6.6)) |
| `get_config` | `GET /config` |
| `get_power_status` | `GET /control/power` — UPS SoC/cell voltage, watchdog armed/on-battery/seconds-lost/grace, active keep-alive hold. Use instead of SSH'ing in to check the `ups-shutdown` watchdog (see [ups_shutdown_watch.py](../scripts/ups_shutdown_watch.py)); `watchdog_running: false` means the systemd unit isn't up, `watchdog_stale: true` means it's up but hung. |
| `get_recent_anomalies` | `GET /anomalies` (`since`/`source`/`mode`/`limit` forwarded; the daemon skips daily files outside the bound) |
| `get_recent_telemetry` | `GET /telemetry/latest` |
| `get_behavior_live` | `GET /models/behavior/live` — `severity` is `null` on an unscored row and `not_scored` names the kind (`off_map`, `actual_stale`, `operating_mode_excluded`, `operating_mode_mismatch`, ...) (B03.9). |
| `get_behavior_models` | `GET /models/behavior` |
| `get_current_capture` | `GET /control/captures/current` |
| `get_bindings` | `GET /bindings` |
| `get_dids` | `GET /dids` |
| `get_obd_coverage` | `GET /obd/coverage` |
| `get_dtc` | `GET /obd/dtc` — cached stored/pending/permanent DTC report (Mode 03/07/0A poll), MIL state, `captured_at`, `stale`. Reports only; there is no dedicated clear-codes tool. |
| `get_monitors` | `GET /obd/monitors` — cached Mode 06 on-board monitor sweep (Task 3.1): per-cylinder misfire counts (`misfire_counts`, cylinder number → raw Mode 06 test value), the full raw `results` list (monitor/test id, value/min/max, `passed`), `captured_at`, `stale`. Reports only. |
| `get_trends` | `GET /control/trends` — per-stat session-fingerprint drift: latest value, reference band, Mann-Kendall trend verdict, CUSUM alarm state, evaluated over the full fingerprint history against the frozen reference epoch. `available: false` when nothing's been frozen yet (`scripts/fingerprint.py freeze`). |
| `get_binned_analysis` | `GET /analysis/binned` — ad-hoc binned aggregation over a value/x[/y] channel triple ("trims by rpm×load") without hand-written SQL or a refit; `agg` is `mean`\|`median`\|`p95`\|`count`, scoped by `session`/`since`/`until` like `query_telemetry`. `available: false` with a `reason` when the scope has no data for the requested channels together. `units=stored` (default) bins raw values; `units=engineering` rescales `decoded:` channels through `bindings.yaml` first — those are raw integers by design (`decoded:ECM_RPM` is 8 counts/rpm), so the default bins them in counts. `decoded:` channels are decoded on read from the raw CAN frames (P02.8: no `decoded` row is stored since 2026-09-26), for old sessions and new alike, for the `confirmed` and `persist: true` bindings. `did:` channels can't reach this path (values live in `value_text`, which the query layer's numeric filter drops) — use `src.analysis.channels.read_channels` locally for those. |
| `get_trip_summary` | `GET /sessions/{id}/trip-summary` — trip-summary sidecar for one session (duration, distance, fuel used, avg economy, CO2, idle time/fraction, max/avg speed, 0-60 mph, 1/4-mile ET/trap if a clean launch was captured). Computed on demand from the stored session and cached to `trip_summary.json` after the first request; also available as a CLI (`scripts/trip_report.py <session>`). |
| `get_session_digest` | `GET /control/sessions/{id}/digest` — end-of-drive digest for one session: duration, mode seconds, gated anomaly (alert) counts by source, evidence-role event counts kept apart (`evidence_counts_by_source`, A06.5; absent on schema-1 digests), top behavior-surface deviations, fingerprint deltas vs. the frozen reference epoch (top movers), new DTCs, new vehicle-ledger events. Authenticated — it aggregates fields (vehicle ledger, fingerprint z-scores) otherwise only readable via `/control/vehicle-ledger` and `/control/trends`. Written by the trip-end hook (sustained GPS moving->stationary, or daemon shutdown as a fallback trigger — see `GpsTracker`/`src.control.trip_end`); computed on demand and cached to `digest.json` if that hook hasn't fired yet for this session. Pushed via ntfy when `notifications.digest_enabled` is set (ships disabled by default). |
| `list_sessions` / `get_session` | `GET /control/sessions[/{id}]`; listing accepts ISO `since`/`until` bounds and a newest-match `limit`, so agents can locate "today's drive" without reading the whole catalog. |
| `get_session_timeseries` | `GET /control/sessions/{id}/chart` — aligned selected `source:CHANNEL` arrays from a stored Pi session without SSH or a Parquet export; defaults to 1 Hz for bounded whole-drive responses. |
| `get_session_classification` | `GET /control/sessions/{id}/classification` — mode/event/disposition sidecar for one session |
| `get_corpus_mode_summary` | `GET /control/classifications/summary` — per-mode seconds + event counts across the corpus |
| `list_events_by_kind` | `GET /control/classifications/events` — sessions containing a given derived event kind (e.g. `cold_start`) |
| `get_dataset_health` | `GET /control/datasets/health` |
| `get_training_runs` | `GET /control/training-runs` |
| `list_vehicle_events` | `GET /control/vehicle-ledger` — vehicle maintenance/intervention ledger (maintenance/mod/repair/observation/DTC), oldest-first, latest per id |
| `list_jobs` / `get_job` / `get_job_logs` | `GET /control/jobs[...]` |
| `get_brain_status` | `GET /control/brain/status` — second brain (E04): ECM program/calibration in force, what a program or calibration change invalidated, selected twin tables, RAM-address standing (re-verify only on a program change), transient ECU state (unknown after a restart), memory and job counts, the live twin and the live next-measurement proposals (`live_proposals`, E03.4) |
| `query_memory` | `GET /control/brain/memory` — `MemoryQuery` over the vehicle's investigation memory (kind, incident, subject, session, regime, calibration/program, valid/as-of times, text, history) |
| `resolve_evidence` | `GET /control/brain/evidence/{id}` — one evidence id resolved, with `part`: ECU computation (twin) or measured engine response |
| `get_investigation_card` | `GET /control/brain/investigations/{id}/card` — the deterministic E01.5 card; `parts` splits its evidence into ECU computation and measured engine response |
| `get_twin_session_evaluation` | `GET /control/brain/twin/sessions/{session}` — the target-phi twin's evaluation of a stored session: accounting, missing inputs, next capture, observed/predicted trace, currency for the program in force |
| `get_ecu_application_model` | `GET /control/ecu/application-model` — the ECM application twin's coverage (E05.1): outputs qualified/conditional, implemented/observed/assumed nodes, blockers, uncovered logic, running image |
| `list_brain_jobs` / `get_brain_job` | `GET /control/brain/jobs[/{id}]` — idle-time jobs (queued/running/deferred/pending/completed/failed, each with its reason). Queue one with `POST /control/brain/jobs` (HTTP only) |
| `diff_did_snapshots` | Compare two completed `did_sweep` job results (see `start_job`'s `did_sweep` kind) — reads each `<job_id>.did_sweep.json` snapshot directly off disk (no HTTP round trip for the diff itself) and classifies every DID that answered in either run: `varying` (differs between runs — the candidates worth investigating), `constant` (same both times), `intermittent` (answered in only one run). Each entry carries the raw hex from each snapshot; most DIDs have no `data/dids.yaml` scale/offset pinned yet |
| `get_tuning` / `get_feedback` | `GET /control/{tuning,feedback}` |
| `tail_daemon_log` | `journalctl --unit canary -o json` (fallback file when no systemd) |
| `get_service_health` | `systemctl show {canary,canary-mcp,ups-shutdown}` — is-active/sub-state/restart-count/uptime, no SSH needed. `restart_count` climbing across polls is the crash-loop tell (this is what would have caught the 2026-07-18 UPS watchdog crash-loop instantly). |
| `search_anomaly_log` | structured search over anomaly day-files + MCP audit log (kind=anomalies\|audit) |
| `signal_lookup` | in-process FSM-name fuzzy match (requires `CANARY_SIGNALS_PATH`) |
| `query_telemetry` | DuckDB over the Parquet ring buffer as written (requires `CANARY_STORAGE_PATH`). No `decoded` rows are stored since 2026-09-26 (P02.8), so a query that asks for them carries a `note` saying so, and an `empty_reason` when it finds none; decode on read instead (below) |
| `get_behavior_deviations` | Local, no daemon call (requires `CANARY_CONFIG`): diffs a *stored* session against the learned behavior surfaces via [`src/analysis/session_deviations.py`](../src/analysis/session_deviations.py) — the "did my change move behavior?" tool. Unlike `get_behavior_live` (current tick only), this replays a whole recorded drive through the training pipeline and reports per-surface n_observed/n_in_domain, mean actual/expected/deviation, warning/critical fraction, and (surface_2d) a per-(rpm,load)-bin breakdown. `mode=` filters to surfaces gated on that `operating_mode`. `against=<session_id>` (Task 5.4) A/Bs `session` directly against a second session instead — per-binned-cell *deltas* (`b - a`, cells with `min_n` rows on both sides) and `matched_deviation_delta`, their mean — never a whole-session mean, which moves with the drives' operating mix (R05.6) — for a before-mod/after-mod comparison. Also available as a CLI: `scripts/analyze_session.py --session <id> [--mode <m>] [--against <id2>] [--profile <slug>]`. |
| `get_delivery_error` | Local, no daemon call (requires `CANARY_CONFIG`): measured-vs-commanded AFR at enrichment for one stored session, with the methodology gates applied *and reported*. Since R05.16 target and measurement are paired by acquisition time (`pairing` in the response, `engine_version: 2`): each commanded-target read meets at most one measured read within the vehicle's declared separation (`lambda_delivery.yaml`, 250 ms on the Xterra), and only a target steady for its regime's declared dwell and clear of that regime's post-fuel-cut holdoff carries a residual (`eligibility.regimes`: on the Xterra enrichment uses a 0.6 s / 0.01 λ dwell and a 6 s holdoff, informed by named drives and judged on held-out ones; `pairing.session_informed_constants` says when the session is one of the informing drives). Transport delay is declared per commanded source: the default Mode 01 route (PID 0x44, batched with the λ read) models none and says so (`pairing.transport_delay_modelled: false`); a route with a frozen delay is re-run at both ends of its bracket and `delay_sensitivity.stable` is false when the answer's sign or existence changes across it — the 1 s last-value grid it replaced scored a target that stepped inside the cell as a delivery error. Every other sample is a named gate: `fuel_cut`, `post_fuel_cut`, `target_transient`, `source_not_valid_in_regime` (PID 0x44 is declared valid only for enrichment), `not_enrichment`, `closed_loop` (`FUEL_STATUS` not open loop), `below_min_load`, `outside_rpm_bands` and the rest; closed loop dilutes any fuelling effect to zero by construction (this is how a real +0.95% K step read as "unchanged"). Bands carry `p05_pct`/`p95_pct` beside the mean. `measured_spec` must be a measured source the declaration names (refused otherwise), and is explicit because the ECM's own λ (PID 0x24) and the A/F-voltage path disagree by ~3.8% at WOT and neither is established as truth. A vehicle without `lambda_delivery.yaml` returns `ok: false`. The `cell_agg` parameter is gone. Both pooled and band-averaged figures are returned; when they disagree, trust the band-averaged one. `min_load` (engine-load floor, %) defaults to the vehicle profile's WOT floor, `mode01_wot.load_floor_pct` in its `tuning_providers.yaml` (operator decision 2026-09-25, R05.13); the floor used is echoed as `min_load` with `min_load_source` `profile` or `caller`, and a profile without `mode01_wot` returns `ok: false` rather than a guessed floor. Also a CLI: `scripts/tuning_report.py delivery-error` (`--min-load` defaults the same way). |
| `compare_tuning_sessions` | Local: A/B two stored sessions on a matched grid. **Refuses `ENGINE_LOAD`/`CAL/LD` as an axis** — it is derived from BFS, so gridding on it while fuelling moves partly conditions on what moved, and it shifted 1.43 points at identical airflow across a same-tune null run; grid on `rpm × MAF` or `rpm × BFS` instead (`unsafe_axes` overrides deliberately). `floor_channel` names a measured noise floor in the vehicle's `noise_floor.yaml` and **requires `conditions`**, the regime this comparison was made under (without it the call returns `ok: false`). The floor is applied only when its recorded `conditions` match exactly: then each cell carries `delta_sigma` + `distinguishable`. On a regime mismatch the floor is **refused**: those fields read null and `floor_refusal` (`kind: conditions_mismatch`, `floor_conditions`, `requested_conditions`, `reason`) names both regimes, because a WOT floor quoted against an idle comparison understates noise ~16× (R05.2). `floor_conditions` / `comparison_conditions` travel with the result. Without `floor_channel`, significance reads null rather than an invented one. Also `scripts/tuning_report.py compare --floor-channel … --conditions …`. |
| `preview_rom_edit` | `POST /control/rom/library/{sha}/edits/preview` (W09) — `sha`, `label`, `edits` (E08.1 `Edit` objects). The edit spec and the edited tables for a ROM library image: `spec_yaml` + `spec_sha256` (what to keep; the same input the laptop simulator takes), `provenance` (old → new raw values per cell, changed ranges, `effect.changed_nodes` / `moved_lookups`), `tables` (the candidate's edited tables), `sim_base` (`ok: false` unless the image is a recorded revision). Nothing is stored, added to the library or offered for download; a candidate is `flashable: false`. Editable: Map 1 (`slot: 1`) `fuel_targets` / `fuel_compensation` / `k_constant`. Refusals raise with the daemon's body: 400 `edit_refused` (`kind`, `reason`, `message`), 404, 409 (no map/definition), 422. A **read tool** (no audit entry: it changes nothing the daemon keeps). |
| `simulate_rom_edit` | `POST /control/rom/library/{sha}/simulate` (W09) — `sha`, `request`: one `SimRequest` object that has **no defaults**, so the tool invents none (a missing field is a 422 naming it, an unknown one `extra_forbidden`). All 21 fields are required: `label`, `edits`, `grid` (`fuel_comp`\|`target_afr`), `rpm_edges`, `load_edges`, `load_basis`, `baro_kpa`, `iat_c`, `coolant_c`, `battery_v`, `rail_kpa` (number or `"working_assumption"`), `throttle_pct`, `pedal_flag` (int 0-255), `stft_pct`, `ltft_pct`, `throttle_closed_learned`, `idle_air_learned`, `evap_purge_area`, `candidate_events` (`none`\|`ecu_reflash`), `draws` (int ≥ 1, ≤ 2000), `seed` (int ≥ 0); grid ≤ 1024 cells. Paired Monte Carlo of the edit on the stated grid, run in the workbench child (up to 900 s; the call waits it out). The base must be a recorded revision. Returns the daemon's body with `cells.cells` cut to the cells a design point visited (`visited_only=True`, the default): the full body is about 370 KB (every cell of a 16 × 16 table), far over many MCP clients' per-result limit, and most cells are unvisited. `cells.cells_omitted` counts the dropped cells and `cells.cells_filter` says so; every other key (the axes, `status_counts`, `overlay`, `reach`, `unplaced`, and `caveats` / `assumptions` first) is the daemon's, untouched, so positions and totals still account for the whole grid. A realistic 256-cell, 6-visited body measured 394,541 bytes full and 31,992 filtered. Pass `visited_only=False` for the daemon's body byte for byte. **Coarse vs fine:** for a quick check send every other Fuel Comp breakpoint on each axis (about a quarter of the points) and `draws: 200`; for a thorough run the full suggested edges and `draws: 1000`. The dev laptop measured 4.5 s for 224 points × 200 draws and 8.5 s for 224 × 1,000 (W09.6; the Pi is not measured). A coarse run's unvisited cells read `no_points`, which is not `unchanged`, and quantiles over few draws are noisy (E08.3); details in headless-api.md. Refusals raise with the daemon's body: 400 `sim_refused` (`kind`, `reason`, `message`), 404, 409 `analysis_deferred` (a capture is recording; `laptop_path`) / `analysis_busy` (retry), 422, 503 `analysis_unavailable`. A **read tool** (no audit entry; an audit record would carry the whole response). Wire contract: [headless-api.md](headless-api.md) "ROM Editor: preview and simulate (W09)". |

Write tools (audit-logged with `actor="mcp"` via
[`src/control/audit.py`](../src/control/audit.py)):

| Tool | Wraps |
|---|---|
| `start_capture` / `tag_current_capture` / `stop_capture` | `POST /control/captures[...]` |
| `record_feedback` | `POST /control/feedback` |
| `record_vehicle_event` | `POST /control/vehicle-ledger` — append a maintenance/mod/repair/observation entry (auto-appended for a new stored DTC too, see `src/control/dtc_state.py`); `corpus_boundary=True` flags an event that changes what "normal" looks like, for `--since-boundary` on `scripts/retrain.py` / `scripts/refit_behavior_models.py`; `event_id` replaces that entry on disk (a typo or wrong reading is gone, not superseded). `intervention` (`hardware` \| `fuel` \| `instrumentation`) classifies a change for investigation memory (E01.4); a correction must re-state it, since the whole entry is replaced. Removing one outright is HTTP-only: `DELETE /control/vehicle-ledger/{id}` (the dashboard's Vehicle Ledger page) |
| `set_session_metadata` / `pin_session` / `unpin_session` | `POST /control/sessions/{id}[...]` |
| `classify_session` | `POST /control/sessions/{id}/classification` — (re)run the session classifier and persist the sidecar |
| `get_capture_quality` | `GET /control/capture/quality[/{session_id}]` — the capture contract verdict: live for the active capture (`scope: live`), or recomputed from a stored session's rows (never rewritten) with the stored sidecar beside it. `failures[].clause` names the constraint that bound (`unanswered_input`, `rate_shortfall`, `acquisition_gap`, `tuple_incomplete`, `tuple_skew`, `persistence_loss`, `unknown_loss_counters`); a v1 sidecar is reported with `scope: trailing_snapshot`. `batching` counters are this capture's arm-to-now delta (`batching.scope: capture`; `status` `verified` / `reset` / `unknown`, counters `null` unless verified; cumulative totals only under `batching.lifetime`). Definitions in `docs/capture-quality.md` |
| `start_job` / `cancel_job` | `POST /control/jobs[...]`. `start_job(kind="did_sweep", params={"start":..., "end":..., "label":...})` runs one unattended Mode 22 DID sweep pass (Canable-only), tagged `label` — run it twice (once per condition) and diff with `diff_did_snapshots` |
| `set_tuning` / `estimate_tuning` / `revert_tuning` | `POST /control/tuning[...]` |
| `set_ups_keepalive` | `POST /control/power/keepalive` — set (minutes>0) or cancel (minutes=0) the UPS watchdog keep-alive hold, for a bench transition longer than the watchdog's grace window |
| `set_tuning_mode` | `POST /control/tuning-mode` — arm/disarm the focused tuning poll profile. Restricts Mode 01 to a small tuning set and suspends DID windows so each channel gets far more of the bus budget. Channels outside the set stop updating while armed — expected, not a fault |
| `arm_tuning_capture` / `disarm_tuning_capture` | Start a capture profile and optional bus-budget override in one daemon transaction, then **verify**: after `verify_seconds` (default 20) the response carries the capture-quality v2 verdict (`scope: live`, `failing_constraints`, per-channel cadence / coverage, `tuples`, and losses scoped to the capture or marked unknown) — see `docs/capture-quality.md`. Rate and budget are separate knobs and both are needed — raising the budget alone does nothing, and raising demand alone just queues. `rate_hz` overrides the profile's own target for that capture only, leaving the saved profile untouched. Repeating an identical arm returns the active capture (`idempotent: true`); disarm restores the exact prior budget state by default and returns `ended_capture_dir`, and an immediate retry returns that same result without touching the budget. Clearing the restored override is explicit. |
| `set_passive_mode` | `POST /control/passive` — arm/disarm receive-only mode. Daemon keeps logging CAN and scoring anomalies but transmits nothing. Arm whenever another tool owns a diagnostic session (ECU reflash, actuator test). OBD channels go stale while armed — expected, not a fault; `get_status` reports `passive.enabled`. `ttl_seconds` (arming only, capped at 3600) auto-disarms after that long even if nobody calls this again — a background daemon task polls for expiry — so set it whenever the window is known in advance instead of relying on remembering to disarm; `passive.ttl_remaining_seconds` in the status payload shows the countdown |
| `trigger_poll` | `POST /control/trigger_poll` — force specific channels due on the OBD scheduler's next tick instead of waiting out their tier cadence (DTC/Mode 06 sit on a 5-minute interval by default). Channel names match `get_status`'s `obd_scheduler.requests[].channel` (`RPM`, `DTC_STORED`, `MONITOR_RESULTS`, `DID_1101`, ...), matched case-insensitively; an unknown name is silently never matched. Only nudges the scheduler — read the result back with `get_status`/`get_dtc`/`get_recent_telemetry`/`query_telemetry` a few seconds later. 409 when `collection.obd_source != canable`, 503 when the collector isn't running |
| `can_activity` | `POST /control/can_activity` — capture N seconds (capped at 30) of raw CAN traffic; per-arbitration-id frame rate plus which byte positions actually changed. The RE workhorse: bracket a physical action (press the brake, flip a switch — tag what you *did*, per AGENTS.md's "tag physical actions" convention) and `changed_byte_indices` on each id answers "which ID carries that signal," without an offline candump-and-diff round trip. `ids` restricts the capture to specific arbitration ids. Pure listen — no frames transmitted, no interaction with passive mode |
| `obd_request` | `POST /control/obd_request` — one-shot OBD/UDS request. `service` accepts any service byte — read, write and actuation alike; nothing filters it. Ask for one specific `service`/`identifier` immediately instead of waiting for the poll scheduler's cadence — Mode 06/DTC sit on a 5-minute tier. `header` overrides the default functional request id (`0x7DF`) to address a specific module (`0x7E0` for the engine ECM, or another module's tester address — opens up non-ECM modules nothing else reaches). Decodes the response when a Mode 01/02 PID, Mode 03/07/0A DTC list, Mode 09 ASCII InfoType, or catalogued Mode 22 DID matches; otherwise returns `raw_hex` only. `responded: false` means no answer in time, not necessarily "unsupported" — arm `set_passive_mode` first if another tool owns a diagnostic session, since this transmits like any other OBD request |
| `upload_model_artifact` | base64-encodes a local artifact + `POST /control/models/{type}/artifact` |
| `replay_window` | submits a `replay_window` daemon job, polls until done, returns the inference report |
| `start_replay` / `stop_replay` / `get_replay_status` | `POST /control/replay[...]` — runtime replay-into-collector with isolated outputs (see "End-to-end session replay" below) |
| `list_capture_profiles` | list saved collection modes for the active vehicle |
| `start_capture` / `stop_capture` | pass `profile` to rotate into a named capture session; stop returns to `default` (without `profile`, the legacy labeled-window behavior remains available) |

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

**Live input is withheld for the whole replay** (D02.9): the collectors
keep running, but their rows stop at a closed ingress and are counted per
source in `live_rows_withheld` instead of interleaving into
`_replay-<id>/`. Start refuses outright on `engine_running`.

**Lifecycle.** A `loop: false` replay ends on its own when its pass runs
out of source: the daemon leaves replay mode, reopens live input, and
`get_replay_status` reads `running: false` with `ended: "completed"` and
that replay's final counters and identity (`rows_emitted`, `passes`,
`source_time_progress`, `effective_speed`, `pass_reconciled`, `replay_id`,
`output_session_dir`, ...). `loop: true` runs until `stop_replay`, which
records `ended: "stopped"`; a replay whose task raised reads
`ended: "failed"`. `stop_replay` after a replay already ended returns that
final status rather than an error, and publishes a second `replay.stop`
control event (outcome `ok`) for a replay that had already exited; only "no
replay since the daemon started" is a 409. A stop that arrives while the
replay is awaiting a chunk read (or its source probes, before the first
row) takes effect once that read returns — up to one chunk load. Before 2026-10-05 a non-looping replay idled at the end
of its pass and stayed `running: true` — with live input withheld — until
someone called stop.

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

**No `decoded` rows are stored (P02.8, operator decision 2026-09-26).** The
raw CAN frames, with their payloads, are the record. A session recorded
since then has no `source = 'decoded'` rows at all, and an older session's
are the decode of its recording day, not the current `bindings.yaml`. The
session readers decode the frames on read (the digest, classification,
`get_binned_analysis`, `get_session_timeseries`, `read_channels`); for SQL, a
query that names `decoded` gets a `note` saying so and, when empty, an
`empty_reason`. To read a broadcast byte in SQL, slice the payload:

```sql
-- 0x1F9 bytes 2-3 (ECM_RPM, raw counts, 8 per rpm), decoded in the query
SELECT timestamp,
       CAST('0x' || substring(hex(payload), 5, 4) AS UBIGINT) / 8.0 AS rpm
FROM telemetry
WHERE source = 'can' AND channel = '0x1F9' AND octet_length(payload) >= 4
ORDER BY timestamp;
```

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

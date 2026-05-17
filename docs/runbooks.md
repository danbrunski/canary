# CANary operator runbooks (Task 6.30)

Task-oriented "how do I…" recipes. Each gives the **dashboard path** and the
equivalent **CLI / `canaryctl`** command. `canaryctl` talks to the Pi's
authenticated control plane over HTTP (no SSH); it reads the host + token from
`~/.canary/env` or `--host` / `--token`. Read-only telemetry needs no token.

Honesty note: some flows are backend-complete but their dashboard panel or
live-ECU path is still pending — that's called out per runbook rather than
implied to work. Clear-DTC and proprietary diagnostic *tests* are deliberately
**not yet implemented** (Tasks 6.5 / 6.18); their safety contract is documented
here so the constraint is unambiguous.

---

## Collect data during this drive

Capture a labeled session for training/RE without SSH.

- **Dashboard:** Control view → *Captures* → Start (set purpose / vehicle
  state) → drive → Tag at events → Stop. (Control panel exists; richer
  capture UI is Phase 6B.)
- **CLI:**
  ```
  canaryctl capture start --purpose "highway baseline" --vehicle-state driving
  canaryctl tag "wot pull"
  canaryctl capture current
  canaryctl capture stop
  ```
- Pre-daemon / RE rig: `scripts/record.py` + `scripts/guided_session.py`
  (`rev` / `nt510` / `drive` protocols, physical-action tags).

## Debug a missing PID

"Why don't I see channel X?"

- **API:** `GET /obd/coverage` (no token) → each registry Mode 01 PID is
  `decoded` / `supported_not_decoded` / `unsupported`, plus poll tier,
  responded-recently, backed-off, and the explicit `undecoded_supported`
  gap list. `supported_not_decoded` = the ECU answers but CANary has no
  decoder yet (an honest gap, never hidden — Risk R7).
- A PID absent from the newest session's `obd:` channels and `unsupported`
  in coverage = the ECU doesn't report it on this vehicle. If `decoded` but
  stale, check `GET /status` → `health.data_freshness_seconds` and the
  scheduler/backoff state.

## Scan DTCs (stored / pending / permanent)

- **Decode/report (backend, no truck):** `src/decode/dtc_catalog.py` —
  `describe_dtc("P0420")` and `build_dtc_report(stored=…, pending=…,
  permanent=…, mil_on=…)` produce a described, timestamped, exportable
  snapshot (curated generic text + structural fallback for unknown/OEM
  codes).
- **Live read (Mode 03/07/0A) + dashboard panel:** pending — needs the
  in-truck ECU path. `CanObdClient.query_mode_03()` + `parse_dtc_payload`
  are the building blocks.

## Clear a DTC while parked

**Not implemented (Task 6.5).** When built, Mode 04 will be the *only*
write CANary makes and must be: behind the authenticated control plane,
parked-interlocked (never available while moving), explicitly confirmed in
browser/CLI, audit-logged, and followed by an automatic rescan. Until then
CANary is strictly read-only — use a dedicated scan tool to clear codes.

## Capture Nissan proprietary DIDs

- **Tooling (works today):**
  `scripts/record.py --obd-source canable --obd-dids 1208,F190 --obd-did-header 7E0`
  issues read-only Mode 22 reads over the Canable into `pids.jsonl`.
  `tools/scan_constant_dids.py --session C0` sweeps the `10 C0` DID space;
  `data/dids.yaml` + `src/decode/dids.py` decode known DIDs.
- **First-class live DID telemetry profile + dashboard switching:** pending
  (Task 6.15) — `10 C0` is mutually exclusive with Mode 01 so it needs
  explicit profile scheduling, not silent mixing.

## Run a parked diagnostic / actuator test

**Not implemented (Task 6.18).** Contract for when it lands: allow-listed
tests only, parked-interlocked, authenticated, countdown + confirmation,
audit log, timeout, post-test restore + rescan. **No** arbitrary raw
CAN/UDS send endpoint will ever be exposed.

## Label a false alarm

Mark an anomaly's verdict so tuning/retraining can use it.

- **Dashboard:** Anomaly timeline → an event already shows its current
  verdict inline (the `/anomalies` feed is feedback-annotated); the
  click-to-label control is Phase 6B.
- **API:**
  ```
  POST /control/feedback   {"event": <anomaly event>, "label":
      "false_positive", "reason": "A/C compressor cycle"}
  GET  /control/feedback   # current verdicts + summary
  ```
  Labels persist append-only next to the anomaly log (full relabel chain
  kept) and export via `feedback_to_csv` for training-set curation.

## Tune a threshold / suppress a known-benign alert

- **API:** `GET /control/tuning` → edit → **estimate before committing**
  `POST /control/tuning/estimate` (before/after FP count over recent
  anomalies, Risk R11) → `POST /control/tuning` (auto-versioned, audited,
  takes effect on the next inference tick) → `POST /control/tuning/revert
  {"version": N}` to roll back (revert is itself a new audited version).
- Suppression counts surface in `GET /status` →
  `emitter_counters.suppressed_tuning`.

## Check whether the corpus is ready to retrain

- **CLI:** `scripts/dataset_health.py` (text) or `--json` (exit 2 = not
  ready). Per-session coverage/mode/value-range/gap/quality + a
  go/no-go verdict with reasons.
- **API:** `GET /control/datasets/health`.

## Export a training dataset

- **CLI:**
  ```
  scripts/export_dataset.py --session <id> --session <id> \
      --format parquet --format feature_matrix --resample-hz 1 \
      --purpose "lstm baseline"
  scripts/export_dataset.py --mark-candidate <id> --purpose "clean highway"
  ```
- Every export carries `dataset_manifest.json` (vehicle, config hash,
  channel schema, sessions, tags, time range, data-quality warnings —
  Risk R12) so a copy taken off the Pi stays reproducible.

## Retrain and publish a model

- **CLI (on the Pi or laptop):**
  ```
  scripts/retrain.py --days 14            # IF + LSTM, ledger-recorded
  scripts/ledger.py list                  # training-run history
  scripts/ledger.py publish <run_id> --status active
  ```
  The daemon's `ModelManager` hot-swaps a new artifact on its next poll
  (no restart) once the feature schema matches.
- **Remote (laptop → Pi, no SSH):**
  ```
  canaryctl jobs start retrain_all --days 14
  canaryctl jobs logs <job_id>
  canaryctl models upload <artifact> <manifest>
  ```
- Companion / offline: artifacts + history live in the local library
  (`~/.canary/library`, Task 6.32); the publish queue is Task 6.37.

---

See also: `docs/headless-api.md` (endpoint reference + SSH-tunnel dev),
`docs/can-re-workflow.md` (RE capture + analysis), `docs/pi-setup.md`
(deployment), `docs/companion-app-plan.md` (offline workflow).

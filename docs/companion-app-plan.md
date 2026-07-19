# Standalone Companion App Plan

CANary's Pi dashboard is the live in-vehicle control surface. It is only
available while the Raspberry Pi is powered in the truck. That creates a gap
for model work: a user may collect several drives, shut the truck off, then
want to review data, build training sets, retrain neural-network models, and
publish the resulting artifacts later.

The companion app fills that gap, but it should not feel like a second product.
It should be the same CANary app shell, visual system, dashboard components, and
control vocabulary running against a different data source. When opened from the
Pi, CANary is in `Live Pi` mode. When opened locally on a laptop/tablet, CANary
is in `Local Library` mode and can still review data, run training, and queue
model publishing.

## Product Boundary

The companion app is not a second vehicle daemon and does not own the CAN
adapter. The Pi remains the only live collector and the only process that talks
to the OBD/CAN hardware in normal operation. The companion app is a second
runtime target for the same CANary frontend, not a separate look and feel.

The CANary app in `Local Library` mode owns:

- offline session library and search
- local telemetry review, charting, and export
- training dataset assembly
- training jobs and metrics review, with an execution target selector
  (`Run on this laptop` or `Run on Pi`)
- model artifact publishing back to the Pi
- queued actions for the next time the Pi is reachable

The CANary app in `Live Pi` mode owns:

- real-time telemetry and control while the truck is on
- capture start/stop/tag while driving
- parked diagnostics and any OBD/proprietary control actions
- live model inference and alerts on the vehicle

## Architecture

The architecture that keeps this manageable is: **one app, one domain model,
two backend providers**.

```text
                  CANary React/TypeScript app
     telemetry | sessions | charts | jobs | models | settings
                         |
              DataProvider / JobProvider boundary
                  /                               \
         LivePiProvider                    LocalLibraryProvider
      Pi FastAPI daemon API              local FastAPI companion API
      live streams + control             SQLite index + files + jobs
                  \                               /
          shared schemas, manifests, model bundles, job specs
```

Key rules:

- The frontend should not know whether a chart is backed by Pi storage or a
  local library except for explicit source/status labels.
- The Pi daemon and the local companion API should expose the same shapes for
  sessions, telemetry history, anomalies, jobs, training runs, and model
  manifests wherever possible.
- Training is a job spec with a target, not a separate workflow:
  `target=local` runs through the local companion API, while `target=pi`
  submits the same logical job to `/control/jobs`.
- Sync is file/bundle based, not database replication. Session bundles and
  model bundles carry manifests, checksums, source identity, config hash, and
  schema metadata.
- The local library index is an acceleration and search layer. The Parquet,
  JSONL, manifests, and model artifacts remain normal files that the existing
  CANary tools can read.

This does add a real sync/offline layer, but it avoids the larger complexity of
duplicating the dashboard, keeping the Pi reachable while powered off, or
inventing separate training workflows for Pi and laptop.

## Primary Workflow

1. **Collect in the truck.** The Pi boots with ignition, starts `canary.service`,
   records CAN/OBD telemetry, runs live inference, and serves the dashboard.
   The user starts/tags/stops captures from the phone dashboard or laptop.
2. **Sync before shutdown.** While the Pi is reachable, the companion app lists
   sessions, downloads selected session bundles, verifies hashes, and records
   local library metadata. Important sessions can be pinned on the Pi before
   retention cleanup.
3. **Work offline.** With the truck off, the user opens the same CANary app on a
   laptop/tablet in `Local Library` mode, reviews telemetry, builds datasets,
   labels false positives, and runs IF/LSTM/behavior-model training jobs using
   synced data.
4. **Publish later.** When the truck is on again, the companion app reconnects
   to the Pi, validates model compatibility, uploads model artifacts/manifests,
   and records the publish event in both the local library and the Pi audit log.

## Recommended Implementation

Start with a laptop companion app because neural-network training and large
Parquet session review are workstation-class tasks. Phone/tablet support should
follow once the data-sync and local-library contracts are stable.

Recommended first implementation:

- use one React/TypeScript dashboard app, not two UIs
- factor API access behind data-provider adapters: `LivePiProvider` for the Pi
  daemon and `LocalLibraryProvider` for the local companion API
- keep telemetry, charting, models, jobs, anomalies, sessions, training, and
  settings panels shared across both providers
- keep one design system, navigation model, channel naming model, and command
  vocabulary
- add a local "library mode" API that reads from a local CANary data root
  instead of the Pi daemon
- run that API as a local FastAPI process from the repo or from a packaged app
- use the existing Python training code directly for local training jobs, and
  use the Pi `/control/jobs` API when the selected training target is the Pi
- keep packaging simple at first: `scripts/canary_companion.py` starts the
  local API and opens the same built frontend against `localhost`

Later packaging options:

- Tauri/Electron desktop shell for Linux/macOS/Windows, with the local FastAPI
  process bundled or supervised
- mobile/tablet app or Capacitor wrapper focused on data review, labeling,
  queued uploads, and light exports
- browser-only PWA mode for review of already-downloaded bundles, if storage
  and file-access limits are acceptable

## Local Data Model

The companion app should store data under a local library root, for example:

```text
~/.canary/library/
  library.sqlite
  sessions/
    2026-05-17T120000/
      manifest.json
      control.json
      tags.jsonl
      anomalies.jsonl
      telemetry-000000.parquet
      frames.log
      pids.jsonl
      checksums.json
  datasets/
  training-runs/
  models/
  sync/
```

`library.sqlite` should index sessions, captures, tags, channel inventory,
quality checks, training datasets, training runs, model artifacts, publish
history, and queued Pi actions. The Parquet/session files remain normal files so
the existing training pipeline can read them without a database dependency.

Session bundles downloaded from the Pi should be content-addressable enough to
avoid duplicate imports:

- stable session ID
- source Pi identity
- source git SHA / CANary version
- config hash
- per-file size and checksum
- exported-at timestamp
- vehicle identity
- channel schema and units

## Sync Contract With The Pi

The existing control plane already has most of the required primitives:

- `GET /control/sessions`
- `GET /control/sessions/{id}`
- `GET /control/sessions/{id}/export`
- `POST /control/sessions/{id}/pin`
- `POST /control/jobs`
- `GET /control/jobs/{id}`
- `GET /control/jobs/{id}/logs`
- `POST /control/models/{type}/artifact`

Since shipped on the companion side (`src/companion/routes.py` /
`services.py`, run via `scripts/canary_companion.py`): session sync
(`POST /companion/sync/sessions/{id}`, `/companion/sessions/import`) and
model publish to the Pi (`publish_model_to_pi`,
`upload_local_model_artifact`), backed by the SQLite library
(`src/companion/library.py`).

Additional sync-oriented API work still needed:

- session inventory fields for byte size, file checksums, channel count,
  capture labels, anomaly count, and export readiness
- resumable or chunked session downloads for large drive captures
- filtered exports by capture window, channel set, and resample rate
- Pi-side "model received / loaded / active" status after upload

The companion app should treat sync as repeatable. If a download or upload is
interrupted, rerunning sync should continue or safely replace only incomplete
local artifacts.

## Training Target Workflow

The user should choose where training runs from the same training screen:
`Run on this laptop` or `Run on Pi`.

Laptop training should use the same pipeline code as Pi training:

1. select sessions or capture windows from the local library
2. run dataset health checks: channel availability, time gaps, operating-mode
   coverage, missing-feature rate, tags, and false-positive labels
3. build an immutable dataset manifest
4. run `retrain_if`, `retrain_lstm`, `retrain_all`, or behavior-model refits
   against the local library
5. store training logs, metrics, artifacts, manifests, false-positive
   benchmark reports, and plots under `training-runs/`
6. compare the new model against the currently published model
7. mark a model as "approved for publish" before upload

Pi training uses the same app screen but submits an authenticated
`/control/jobs` request to the Pi and streams job status/logs from the Pi
control API. The resulting model can be published immediately on the Pi or
synced back into the local library for comparison and recordkeeping.

Both targets should produce the same `TrainingRun` metadata shape so the model
metrics dashboard does not care where the compute ran.

The companion app should not mutate a live model in place. Every trained model
gets a versioned artifact, a manifest, and a rollback path.

## Offline UI Requirements

When the Pi is off, the companion app should still provide:

- the same CANary visual design, navigation, charts, gauges, model panels, and
  terminology as the Pi-hosted dashboard
- session library with search/filter by date, route, tags, operating mode,
  data quality, and vehicle
- telemetry explorer and charts over synced data
- anomaly timeline and false-positive labeling
- dataset builder and export tools
- training job launcher, progress, logs, and cancellation
- training metrics and model-health views
- model comparison and publish queue
- Pi connection profiles and sync status

When the Pi is online, the same app can show a "Live Truck" connection with
preflight, capture controls, session sync, and model publish. The UI should
make the active data source and compute target explicit: `Live Pi`,
`Local Library`, `Queued Offline`, `Training on Pi`, or
`Training on this laptop`.

## Security and Safety

- Store Pi control tokens in the OS keychain where available; otherwise use a
  file with restrictive permissions and never include tokens in exported
  bundles.
- Require explicit approval before uploading a model to the Pi.
- Validate model schema compatibility before upload and again on the Pi before
  activation.
- Keep parked diagnostic actions on the Pi dashboard/control plane only; the
  companion app may queue requests, but execution still requires the Pi to be
  online and the vehicle to satisfy parked interlocks.
- Preserve audit logs for sync, training, labeling, threshold changes, and
  model publishing.

## Acceptance Criteria

The standalone companion workflow is complete when:

1. A user can collect a drive on the Pi, sync the session to a laptop, shut the
   truck off, and still open the CANary app locally.
2. The local app can browse, chart, label, and export the synced session without
   contacting the Pi, using the same look and feel as the Pi-hosted dashboard.
3. From the same training UI, the user can choose to run at least IF and LSTM
   retraining locally against synced sessions or remotely on the Pi when it is
   reachable, and display training metrics for either target.
4. The local app can produce a versioned model bundle with manifest, metrics,
   checksums, and compatibility report.
5. When the Pi is powered again, the local app can upload the approved model
   bundle and confirm that the Pi accepted and activated it.
6. Interrupted downloads/uploads can be retried without corrupting the local
   library or Pi model directory.

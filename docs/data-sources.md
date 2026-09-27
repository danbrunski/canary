# Data sources

CANary ingests vehicle telemetry from several distinct channels, each with
its own collection mechanism, decode path, and column namespace. This
document explains the *classes* of data and how each flows from the wire
to a model-ready feature column. (For the specific signal names / CAN IDs /
DID hex, see `data/bindings.yaml`, `data/dbc/`, and `data/dids.yaml` —
those are intentionally not enumerated here.)

## Column namespace at a glance

Every telemetry value lands in a wide-frame column named
`<source>:<channel>`. The prefix tells you where it came from and how it
was obtained:

| Prefix | Class | Mechanism | Decode |
|---|---|---|---|
| `obd:` | Standardized OBD-II | Active poll (request/response) | Standard formula, real units |
| `did:` | Nissan Mode-22 DID | Active poll (request/response) | `data/dids.yaml` scale/offset |
| `decoded:` | Broadcast CAN | Passive listen | `data/bindings.yaml` (reverse-engineered) |
| `dbc:` | Broadcast CAN | Passive listen | `data/dbc/*.dbc` (standard DB) |
| `can:` | Raw CAN frame | Passive listen | none (arbitration ID only) |
| `derived:` | Computed | n/a — derived from other columns | formula |
| `deviation:` | Behavior residual | n/a — model vs actual | behavior surface |

## Active vs passive — the core distinction

Two fundamentally different ways CANary gets data off the bus:

**Active (request/response)** — CANary transmits a diagnostic request and
the ECM replies with a value. Rate-limited by the poll scheduler (a bus
budget of ~8 requests/sec at `poll_rate_hz: 1`, tiered fast/medium/slow
per signal). Higher latency (~30 ms round-trip) and lower rate, but
standardized and self-describing. Covers `obd:` and `did:`.

**Passive (listen)** — the vehicle's modules (ECM, BCM, ABS, etc.)
broadcast CAN frames to each other continuously; CANary only listens and
decodes the bytes. Zero added bus load, capture at full broadcast rate
(often 10-100 Hz), zero request latency — but vehicle-specific and only
covers signals someone has reverse-engineered into a binding. Covers
`decoded:`, `dbc:`, and the raw `can:` frames they're decoded from.

## The classes

### `obd:` — standardized OBD-II PIDs

Mode 01 (current data) requests against the ECM. The OBD-II standard
defines each PID's formula, so values arrive pre-calibrated in real units
(rpm, °C, %, kPa). Works on any OBD-II compliant vehicle. This is the
calibration ground-truth — when a signal also exists on the broadcast
bus, the `obd:` version is the cross-check.

Collection: `src/collection/obd_scheduler.py` builds the poll schedule;
`src/collection/can_obd_collector.py` runs the request loop on the
Canable-native path (`src/collection/obd_collector.py` is the legacy
ELM327 path). Decoded values become `TelemetryRecord`s with `source=obd`.

### `did:` — Nissan Mode-22 DIDs

Manufacturer-proprietary diagnostic identifiers, also actively polled but
via UDS service 0x22 instead of standardized Mode 01. They expose signals
OBD-II doesn't — injector pulse width, raw wideband AFR sensor voltage,
per-bank sensor values, raw throttle-position-sensor voltage. The 2008
Nissan ECM only answers Mode 22 inside a non-default diagnostic session
(see the access metadata in `data/dids.yaml`), and that session is
mutually exclusive with standard Mode 01.

A polled DID arrives as a raw row: `source=obd`, `channel=DID_<hex>`,
value = the response payload as a hex string (non-numeric). The decode
step (`src/decode/dids.py:decode_did_rows`) matches it against the DID map
in `data/dids.yaml`, extracts the integer from the declared byte range,
applies scale/offset, and emits a `did:<NAME>` numeric row. When a DID's
scale constant isn't pinned yet, the **raw integer** is emitted (unit
`raw`) — anomaly detection works on deviation from baseline, so unscaled
but consistent counts are still useful; only human-facing units need the
finalized constant.

DIDs to poll are configured in `collection.obd_dids` (canary.yaml). Each
adds bus-budget load, so only the high-value ones are enabled by default.

### `decoded:` — broadcast CAN via bindings.yaml

Signals reverse-engineered from the raw broadcast CAN traffic. Each entry
in `data/bindings.yaml` says which arbitration ID carries the signal,
which bytes, and the scale/offset (the RE work documented in
`docs/can-re-workflow.md`). `src/preprocessing/decode.py:decode_can_rows`
turns each raw `can:` frame into one `decoded:<NAME>` row per matching
binding. Captured at full broadcast rate — these catch transients the
1 Hz OBD poll misses.

### `dbc:` — broadcast CAN via DBC files

Same passive broadcast source as `decoded:`, but decoded through standard
`.dbc` database files (`data/dbc/`) using `cantools` rather than the
hand-built bindings. `src/decode/dbc.py:decode_can_rows_dbc` emits
`dbc:<SIGNAL>` rows. Kept as a separate namespace so the two decode paths
never collide; useful for signals already covered by a published DBC.

### `can:` — raw frames

A raw CAN frame that hasn't been decoded into a named signal — only the
arbitration ID and payload bytes. These exist so the decode steps above
have something to work from; once decoded they carry no numeric value of
their own and are dropped before feature extraction (they'd be all-NaN
columns). Persisted to Parquet so new bindings can be applied
retroactively to old captures.

### `derived:` — computed signals

Values calculated from other columns, not read off the bus. Two producers:

- `src/signals/derived.py:compute_derived_channels` — trip-meter style
  outputs (fuel rate, economy, distance, CO₂) for the dashboard.
- `src/signals/derived.py:compute_anomaly_signals` — physical-residual
  signals for anomaly detection: air mass per cycle, per-bank fuel-trim
  and AFR asymmetries, TPS-voltage-to-percent ratio. Each is gated on its
  inputs existing, so a session without the required PIDs/DIDs skips the
  column cleanly rather than NaN-filling.

### `deviation:` — behavior-model residuals

The difference between a behavior model's expected value and the actual
reading, per tick. Produced by `src/decode/behavior_decoder.py` from the
surfaces in `behavior_models.yaml` (profile-scoped when vehicle profiles
are enabled, e.g. `data/nissan-xterra-2008/behavior_models.yaml`). These feed both the direct
behavior anomaly events and the IF/LSTM feature set (the unsupervised
detectors learn the normal residual distribution). See
`docs/behavior-model-inference.md`.

## Processing pipeline

All sources converge through one preprocessing path so the offline trainer
and the live daemon can never schema-diverge
(`src/preprocessing/feature_pipeline.py`):

```
read raw rows (Parquet offline / queue live)
  → decode_can_rows        (can: → decoded:)
  → decode_can_rows_dbc     (can: → dbc:)
  → decode_did_rows         (obd:DID_<hex> → did:)
  → drop raw can: rows      (dead weight post-decode)
  → to_wide                 (long rows → <source>:<channel> columns)
  → resample_wide           (uniform time grid)
  → segment_operating_modes (operating_mode column: warmup/idle/city/...)
  → compute_anomaly_signals (derived: columns)
  → append_deviation_columns(deviation: columns, when behavior models loaded)
  → compute_features        (rolling __mean / __std / __roc per column)
```

The live path (`src/preprocessing/live.py:LiveFeatureSource`) mirrors this
exactly on a per-tick basis — every decode step above has a streaming
counterpart so the live feature row carries the same columns the model was
trained on. A mismatch would fail the model's feature-spec check.

## Storage

Raw telemetry is written to per-session Parquet under
`collection.storage_path` (`<storage>/<YYYY-MM-DDTHHMMSS>/telemetry-*.parquet`),
rotated per ignition cycle. The schema (`src/storage/parquet_writer.py`)
stores the long-form rows — `timestamp, source, channel, value_number,
value_text, value_bool, unit, payload, session` — so all source classes
share one table and any decode step can be re-run later against the same
raw capture. A byte-bounded retention sweeper evicts the oldest unpinned
sessions; `_`-prefixed directories (`_archive/`, `_replay-*`) are excluded
from training discovery and the sweep.

Operator and auto-detected events live alongside the Parquet as JSONL
sidecars: `tags.jsonl` (operator captures/tags), `derived_tags.jsonl`
(classifier-detected events), and `classification.json` (per-session mode
+ event + disposition summary). See `docs/mcp-server.md` and the session
classifier for those.

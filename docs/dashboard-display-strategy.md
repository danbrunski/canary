# Dashboard Display Strategy

CANary's dashboard should optimize for the hardware that is easiest to use in
the vehicle first: a phone connected to the Pi over the vehicle LAN, optionally
mirrored to the Kenwood head unit. A native Android Auto app is a later,
constrained companion surface rather than the main dashboard.

## Recommended Targets

### Primary: React/TypeScript PWA

Build the full dashboard as a single-page web app served by the Raspberry Pi:

- backend: FastAPI REST + WebSocket endpoints
- frontend: React + TypeScript + Vite
- display: phone browser/PWA in fullscreen mode
- head unit: phone screen mirrored via the receiver's mirroring mode, HDMI,
  Miracast, or equivalent model-specific feature

This keeps the rich UI in normal web technology where CANary can render dense
diagnostic screens, charts, gauges, and maintenance controls without Android
Auto template restrictions.

### Short-Term In-Vehicle Display: Head-Unit Mirroring

Head-unit mirroring is treated as display transport, not an app platform. The
phone opens CANary's local web dashboard and the receiver mirrors the phone
screen. This should work for:

- realtime telemetry gauges
- anomaly timeline and current alert state
- model health and neural-network performance panels
- tire pressure monitor panel, once TPMS data is decoded
- parked/maintenance diagnostics screens

Operational assumptions:

- Pi provides a local Wi-Fi hotspot or joins the vehicle/phone hotspot.
- Dashboard URL is local, for example `http://canary.local:8000`.
- The PWA supports landscape, high-contrast, large-touch layouts.
- Touch passthrough, resolution, and driving lockouts depend on the receiver.

### Later: Native Android Auto Companion

Official Android Auto should be considered a limited companion, not the full
CANary dashboard. It is useful for driver-safe status only:

- system OK / degraded / offline
- active anomaly alert summary
- TPMS warning summary
- model/data freshness status

Do not plan to expose rich arbitrary charts or diagnostic controls through
Android Auto. Keep those in the PWA, and use Android Auto only if its supported
templates can represent the needed driver-safe status view.

## UI Modes

### Driving Mode

Driving mode must be glanceable and passive:

- current status: OK / warning / anomaly
- current operating mode
- key gauges: RPM, speed, coolant, MAF, throttle, voltage if available
- anomaly score sparkline
- TPMS status once available

No proprietary diagnostic controls are exposed in driving mode.

### Parked / Maintenance Mode

Parked mode can expose deeper tools:

- full telemetry channel explorer
- anomaly event inspection
- model performance/debug panels
- reverse-engineered protocol tools
- diagnostic control actions, once implemented

Diagnostic controls need explicit safeguards:

- require parked/maintenance mode
- require explicit confirmation for every action
- separate read-only diagnostics from write/control actions
- log every request and response
- keep controls disabled unless the relevant protocol binding is confirmed

## Graphics Stack

Use React for application structure and state, but avoid rendering high-rate
charts as thousands of DOM nodes. Prefer canvas/WebGL-backed chart components
for realtime series.

Recommended frontend structure:

- `DashboardShell`: layout, connection state, navigation
- `TelemetryPanel`: current values and sparklines
- `AnomalyPanel`: score chart and active event summary
- `ModelPanel`: model version, thresholds, inference latency, reconstruction
  error / Isolation Forest score
- `TpmsPanel`: per-tire pressure/temperature/status once decoded
- `DiagnosticsPanel`: parked-only proprietary protocol tools
- `SystemPanel`: storage, queue pressure, adapter health, data freshness

## Data Flow

The Pi remains the source of truth:

1. collectors write telemetry and publish latest values
2. preprocessing/inference emits current features and anomaly scores
3. FastAPI exposes REST snapshots for initial page load
4. WebSockets stream telemetry, anomalies, system status, and model status
5. React keeps short in-memory ring buffers for charts

The frontend should not read Parquet directly. Historical/session views should
request downsampled API data from the backend.

## Safety Boundary

CANary's default system behavior remains read-only. Any future diagnostic
control path is a separate, gated feature. The dashboard should make that
boundary visible in the UI and enforce it in the backend as well as the
frontend.

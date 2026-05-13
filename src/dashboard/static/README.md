# CANary dashboard frontend

React + TypeScript + Vite PWA. Served by the FastAPI backend in production
(``StaticFiles`` mount on ``/``); dev runs Vite on ``localhost:5173`` and
proxies the backend on ``localhost:8000``.

The runtime architecture is documented in
[`docs/daemon-architecture.md`](../../../docs/daemon-architecture.md);
the UI strategy (PWA + head-unit mirroring; driving / parked modes) is in
[`docs/dashboard-display-strategy.md`](../../../docs/dashboard-display-strategy.md).

## Layout

```
src/dashboard/static/
  package.json                # deps + scripts
  vite.config.ts              # dev proxy for /status, /anomalies, /ws/*
  tsconfig*.json              # strict TS
  index.html                  # entry
  public/                     # static assets (manifest, icon)
  src/
    main.tsx, App.tsx         # bootstrap + page-level state
    types.ts                  # TS shapes mirroring the Python schemas
    api/
      http.ts                 # /status, /config, /anomalies, /telemetry/latest
      websocket.ts            # reconnecting JSON WebSocket
    hooks/
      useStatus.ts            # /status polling
      useTelemetryStream.ts   # /ws/telemetry + per-channel ring buffer
      useAnomalyStream.ts     # /ws/anomalies + recent-events list
    components/
      Sparkline.tsx           # canvas, no chart-lib dep
    panels/
      DashboardShell.tsx      # header, mode toggle, layout
      ConnectionIndicator.tsx # the colored dots
      OperatingModeIndicator  # current operating-mode badge (Task 5.11)
      TelemetryPanel.tsx      # live PIDs + sparklines (Task 5.9)
      AnomalyPanel.tsx        # score chart + event list (Task 5.10)
      ModelPanel.tsx          # loaded versions + emitter counters (Task 5.12)
      SystemPanel.tsx         # storage, freshness, vehicle
      TpmsPanel.tsx           # placeholder until bindings land (Task 5.13)
      DiagnosticsPanel.tsx    # parked-mode only (Task 5.14)
```

## Develop

Pre-req: Node 18+. Anything 20/22 works.

```sh
cd src/dashboard/static
npm install
npm run dev                   # http://localhost:5173
```

In a second shell, run the backend with CORS for the dev origin:

```sh
.venv/bin/python scripts/serve_dashboard.py --config canary.yaml --dev-cors
```

Vite's proxy (configured in `vite.config.ts`) forwards `/status`,
`/config`, `/anomalies`, `/telemetry`, and `/ws/*` to `127.0.0.1:8000`, so
the SPA looks like it's same-origin with the backend.

## Build for production

```sh
cd src/dashboard/static
npm run build                 # → dist/
```

`scripts/serve_dashboard.py` auto-discovers `dist/` next to the backend
package and mounts it on `/`; no extra config needed. Until the build has
run, `/` returns 404 and the API routes still work — useful for backend-only
deployments.

## Typecheck

```sh
npm run typecheck             # strict TS, no emit
```

Runs as part of `npm run build`. CI should call `npm run build` end-to-end
when the frontend is in scope.

## Connection model

Each panel reads from one of three hooks (`useStatus`, `useTelemetryStream`,
`useAnomalyStream`) and renders against the latest snapshot. The hooks
own:

* HTTP seeding (so a panel renders immediately, not after the first WS frame)
* WebSocket subscription with reconnect-on-close (exponential backoff capped at 5 s)
* Per-channel ring buffer (telemetry) / deduped event list (anomalies)
* Connection state surfaced through `ConnectionIndicator` in the header

This is intentionally minimal — no Redux, no React Query — because the
state shape is small and the cost of a render-per-message is low (we'd
notice in `scripts/benchmark_live_pipeline.py`-style flame graphs before
anyone notices it in the UI). Revisit when the panel count or the channel
count outgrows what a single Map can hold cheaply.

## Charts

Sparklines render via a tiny canvas component (`components/Sparkline.tsx`).
Each chart is one canvas node, redrawn on every message — fast enough at
the daemon's 1 Hz cadence, no chart-lib dep, no SVG-thousand-nodes problem.
If a richer chart is ever needed (zoom, brushing, multi-series with
legends), `uPlot` is the recommended choice — also canvas-based, MIT,
no React dependency.

## Driving / parked mode

The mode toggle in the header gates the parked-only `DiagnosticsPanel`.
Driving mode hides the panel entirely (not "shows disabled") so a
mis-tap can't accidentally land on a diagnostic control. The backend
will enforce the same gate when controls are added — the frontend gate
is an ergonomics layer, not the security boundary
(`docs/dashboard-display-strategy.md`).

## What's not built yet

* Tire-pressure panel: stub until `decoded:TPMS_*_PRESSURE` are confirmed bindings.
* Diagnostic controls: surfaces will land in `DiagnosticsPanel` as the proprietary protocol tools come online.
* Behavior-model deviation heatmaps (Task 4.E.9): waiting on the backend feed.
* PWA offline cache + service worker: not needed yet — the Pi is the only origin and it's local.

Each placeholder is a real component that renders a "not available yet"
message; once the backend ships the data, the renderer takes over without
shell changes.

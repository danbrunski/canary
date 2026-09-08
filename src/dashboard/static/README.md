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
      TokenField.tsx          # the token password input, shared by TokenGate
      ResourceGate.tsx        # token/loading/error 3-state every polled panel needs
      TokenGate.tsx           # the ONE control-token entry point (Task 5.1)
    panels/
      DashboardShell.tsx      # header, Drive/Workbench toggle, ReplayBanner + StatusStrip
      ConnectionIndicator.tsx # the colored dots (telemetry/anomaly/control streams)
      OperatingModeIndicator  # current ML operating-mode badge (Task 5.11)
      VehicleStateBadge.tsx   # status.vehicle_state badge (Task 1.1) — read this first
      ReplayBanner.tsx        # full-width "you're looking at replayed data" banner (Task 1.2)
      StatusStrip.tsx         # always-visible spine: source (live/replay), freshness, alerts, model health
      Workbench.tsx           # grouped sidebar nav (see nav.ts) + the active panel
      DriveScreen.tsx         # the locked, glanceable Drive-mode view
      AnomalyPanel.tsx        # score chart + event list + evidence + re-score (Tasks 5.10, 4.1)
      ModelEvidencePanel.tsx  # per-detector evidence for an inspected anomaly
      RescoreControl.tsx      # "re-score this window" replay_window job (Task 4.1)
      SessionsPanel.tsx       # session list/metadata/pin/export/delete + ReplayCard (Tasks 3.1/3.2)
      ReplayCard.tsx          # start/stop/inspect runtime replay from the UI (Task 2.2)
      ControlPanel.tsx        # capture, jobs, detector switch, artifact upload, preflight, companion
      TestSessionPanel.tsx    # in-vehicle "tap an event as it happens" screen
      ... one file per `nav.ts` entry — MonitorsPanel, ExplorePanel, ExportPanel,
          LlmAssistPanel, DtcPanel, TrendsPanel, TripPanel, DatasetHealthPanel,
          CustomPidsPanel, BehaviorModelsPanel, TrainingPanel, TuningPanel,
          ModelHealthPanel, ProfilesPanel, PowerPanel, TpmsPanel, SystemPanel,
          DataSourcesPanel, CustomDashboardPanel, ChartsPanel, AlarmsPanel
```

## Styling — Tailwind CSS v4

Tailwind v4 is the styling system, wired through the official Vite plugin
(`@tailwindcss/vite` in `vite.config.ts`) — no `tailwind.config.js`, no
PostCSS step. The design system is CSS-first: `src/index.css` does
`@import "tailwindcss"` then declares the palette / radii / numeric font in
a single `@theme` block. Tailwind compiles those tokens into **both** utility
classes (`bg-elev`, `text-accent`, `rounded-card`, …) **and** CSS custom
properties (`--color-accent`, …). New / refactored markup uses utilities
directly; the curated panel classes (`.panel`, `.channel-card`, …) are
authored as plain CSS against the same tokens, so the two stay in lockstep
and there's a single source of truth for the look. Change a colour in
`@theme` and utilities + components move together.

## Develop

Pre-req: Node 18+ (20/22 work) and [pnpm](https://pnpm.io) 10+. If pnpm
isn't installed, `corepack enable && corepack prepare pnpm@10.29.3 --activate`
picks up the version pinned in `package.json`'s `packageManager` field.

```sh
cd src/dashboard/static
pnpm install
pnpm dev                   # http://localhost:5173
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
pnpm build                 # → dist/
```

`scripts/serve_dashboard.py` auto-discovers `dist/` next to the backend
package and mounts it on `/`; no extra config needed. Until the build has
run, `/` returns 404 and the API routes still work — useful for backend-only
deployments.

## Typecheck

```sh
pnpm typecheck             # strict TS, no emit
```

Runs as part of `pnpm build`. CI should call `pnpm build` end-to-end
when the frontend is in scope.

## Test

```sh
pnpm test                  # one-shot
pnpm test:watch            # rerun on file change
```

Vitest + React Testing Library + jsdom. Tests live next to the code
they cover (`*.test.ts` / `*.test.tsx`). Current coverage prioritizes
the async/state-machine pieces that are easiest to break:

* `api/websocket.test.ts` — reconnect-with-backoff, JSON parse failure
  handling, `close()` shutdown, constructor-throws path
* `hooks/useAnomalyStream.test.ts` — `/anomalies` seed ordering, WS
  dedupe key, per-source dedupe isolation, connection state
  propagation, unmount cleanup
* `hooks/useTelemetryStream.test.ts` — seed → live merge, 120-sample
  ring-buffer cap, non-numeric values skipped from history, malformed
  rows ignored

Pinned versions: `vitest@^2.1.9` + `jsdom@^25` for Vite 5 compatibility
(Vitest 4 / jsdom 29 require Vite 6+).

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

## Drive / Workbench modes and the Workbench nav

The header toggle switches the *whole* context, not a single panel:

* **Drive** (`DriveScreen.tsx`) — a locked, glanceable live-gauge view for
  while the truck is moving. No controls, no navigation chrome, so a
  mis-tap can't land on a mutating action.
* **Workbench** (`Workbench.tsx`) — the parked diagnostic workbench: a
  grouped sidebar (Live / Diagnostics / Models / Data / Control, see
  `nav.ts` — the single source of truth for "where does a panel live")
  plus the active panel. Every panel has exactly one home there; a new
  panel registers in `nav.ts` and nothing else changes.

Both modes share the same header and the `StatusStrip` pinned beneath it
(`panels/StatusStrip.tsx`) — the always-visible "is everything OK" spine:
live-vs-replay source, telemetry freshness, current anomaly state, model
health, accepted-baseline overrides.

The header itself always shows, regardless of mode:

* `OperatingModeIndicator` — the ML operating mode (idle/city/highway/koeo).
* `VehicleStateBadge` (Task 1.1) — the derived `status.vehicle_state`
  classification (`engine_running` / `ignition_only` / `engine_off` /
  `no_data` / `replay` / `unknown`). This is the field to read *first*
  before concluding anything about detector behavior from silence —
  distinct from the ML operating mode above. `no_data`/`unknown` render
  with alert styling.
* `TokenGate` (Task 5.1) — the **one** control-token entry point (lock
  icon → popover entry → unlocked), backed by `useControlToken` so every
  gated panel shares the same stored token. Replaces the per-panel
  `TokenField` instances `ControlPanel` and `TestSessionPanel` used to
  render independently; the four-state vocabulary ("not needed" /
  "required" / "rejected" / "unlocked") originated in `TestSessionPanel`
  and is now the one place that logic lives.

Whenever a runtime replay (`/control/replay`) is active, a full-width
`ReplayBanner` (Task 1.2) renders above `StatusStrip` in *both* modes —
"REPLAY — session `<id>`" plus elapsed/speed — so re-emitted session data
is never mistaken for the live truck. `ReplayCard` (Task 2.2), hosted in
the Sessions panel beside the session it replays (Task 3.2), starts/stops
replay from the UI and surfaces the daemon's refuse-while-driving 409
verbatim (replay is a bench/desk tool, not something to run while
actually driving).

`AnomalyPanel` → inspect an event → `ModelEvidencePanel` closes the
triage loop with a token-gated "re-score this window" button
(`RescoreControl.tsx`, Task 4.1): submits the same `replay_window` job
the MCP tool wraps, polls it through the existing jobs plumbing, and
renders the per-detector fired/score-vs-threshold report inline.

## What's not built yet

* Tire-pressure panel: stub while the `0x385` pressure bindings remain
  hypotheses. Bytes 2-5 plausibly decode at 0.25 psi/count using an external
  QM5/Koleos layout, but the Xterra scale and corner order need a controlled
  one-tire pressure-change capture before promotion to dashboard-visible confidence.
* Behavior-model deviation heatmaps (Task 4.E.9): waiting on the backend feed.
* PWA offline cache + service worker: not needed yet — the Pi is the only origin and it's local.

Each placeholder is a real component that renders a "not available yet"
message; once the backend ships the data, the renderer takes over without
shell changes.

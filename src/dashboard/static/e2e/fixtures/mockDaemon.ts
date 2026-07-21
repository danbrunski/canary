import type { Page, Route } from "@playwright/test";

import type {
  AnomaliesResponse,
  BehaviorLiveResponse,
  BehaviorModelsResponse,
  StatusResponse,
} from "../../src/types";

/**
 * Network stubs standing in for the live daemon.
 *
 * The dashboard talks to the daemon two ways: polled `GET` JSON endpoints
 * (`/status`, `/models/behavior`, ...) and WebSocket streams (`/ws/telemetry`,
 * ...). We stub both so the app boots deterministically with no backend. The
 * fixture payloads are deliberately minimal-but-valid — the smallest shape
 * each `types.ts` interface accepts — and are exported so individual specs can
 * spread-and-override a field (e.g. flip `vehicle_state`) without rebuilding
 * the whole object.
 */

export const STATUS_FIXTURE: StatusResponse = {
  storage_path: "/data/canary",
  free_mb: 20_480,
  latest_session: "2026-07-20T101500",
  latest_session_path: "/data/canary/2026-07-20T101500",
  model_versions: { isolation_forest: "if-v7", lstm_autoencoder: null },
  vehicle: "2008 Nissan Xterra",
  emitter_counters: {
    emitted: 3,
    suppressed_not_anomaly: 41,
    suppressed_dedupe: 5,
    suppressed_tuning: 0,
  },
  health: null,
  accepted_baseline_overrides: { active: 0, overrides: [] },
};

export const BEHAVIOR_MODELS_FIXTURE: BehaviorModelsResponse = {
  schema_version: 1,
  models: [],
};

export const BEHAVIOR_LIVE_FIXTURE: BehaviorLiveResponse = {
  observed: 0,
  timestamp: null,
  operating_mode: "idle",
  models: {},
};

export const ANOMALIES_FIXTURE: AnomaliesResponse = { events: [], count: 0 };

const json = (route: Route, body: unknown) =>
  route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

/**
 * Route table: first pattern whose `test()` matches a request URL wins. Order
 * matters — specific paths before the `/control/**` catch-all. Anything not
 * listed falls through to a `{}` 200 so a newly-wired fetch never hard-fails
 * the page load; a spec that cares about a specific endpoint should stub it
 * explicitly before calling nothing further.
 */
const ROUTES: ReadonlyArray<[RegExp, unknown]> = [
  [/\/status(\?|$)/, STATUS_FIXTURE],
  [/\/models\/behavior\/live(\?|$)/, BEHAVIOR_LIVE_FIXTURE],
  [/\/models\/behavior(\?|$)/, BEHAVIOR_MODELS_FIXTURE],
  [/\/anomalies(\?|$)/, ANOMALIES_FIXTURE],
  [/\/bindings(\?|$)/, {}],
  [/\/config(\?|$)/, {}],
  [/\/dids(\?|$)/, { dids: [] }],
];

export interface MockDaemonOptions {
  /** Shallow-override fields of the default `/status` payload. */
  status?: Partial<StatusResponse>;
}

/**
 * Install the daemon stubs on `page`. Call before `page.goto`.
 *
 * WebSocket routes are accepted and held open with no frames — the app shows
 * its "connecting"/idle stream state rather than an error, which is the
 * correct resting state for a browser that reached the daemon but has no live
 * telemetry yet.
 */
export async function mockDaemon(
  page: Page,
  options: MockDaemonOptions = {},
): Promise<void> {
  const status: StatusResponse = { ...STATUS_FIXTURE, ...options.status };

  // Accept the WebSocket handshakes and keep them open, sending nothing.
  await page.routeWebSocket(/\/ws\//, () => {
    // No-op: connection stays open, no frames delivered.
  });

  // Intercept every daemon API prefix (mirrors vite.config's dev proxy table)
  // so nothing leaks to the real proxy → :8000 during the run: a known payload
  // where we have one, an empty `{}` 200 otherwise.
  await page.route(
    /\/(status|config|control|anomalies|telemetry|bindings|models|obd|dids|test-profiles)(\/|\?|$)/,
    (route) => {
      const url = route.request().url();
      if (/\/status(\?|$)/.test(url)) return json(route, status);
      for (const [pattern, body] of ROUTES) {
        if (pattern.test(url)) return json(route, body);
      }
      return json(route, {});
    },
  );
}

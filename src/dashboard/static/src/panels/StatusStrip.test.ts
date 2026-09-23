import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  AcceptedBaselineOverrideSummary,
  AnomalyEvent,
  StatusResponse,
} from "../types";
import { StatusStrip } from "./StatusStrip";

function status(
  overrides: StatusResponse["accepted_baseline_overrides"],
): StatusResponse {
  return {
    storage_path: "/tmp/canary",
    free_mb: 2048,
    latest_session: "s1",
    latest_session_path: "/tmp/canary/s1",
    model_versions: {},
    vehicle: "2008 Nissan Xterra",
    emitter_counters: null,
    health: null,
    accepted_baseline_overrides: overrides,
  };
}

const OVERRIDE: AcceptedBaselineOverrideSummary = {
  target: "obd:SHORT_FUEL_TRIM_1",
  channel: "obd:SHORT_FUEL_TRIM_1",
  behavior_model: null,
  operating_mode: "idle",
  accepted_center: 12.0,
  band: {
    kind: "directional",
    tolerance: 3.0,
    worse_direction: "positive",
    worse_warning: 3.0,
    worse_critical: 6.0,
    better_direction: "ignore",
  },
  declared_by: "dan",
  declared_at: "2026-05-17",
  review_by: "2026-11-17",
  reason: "known weak #3 injector; owner not replacing",
  expired: false,
};

describe("StatusStrip — replay source label (Task 1.2)", () => {
  it("reads 'live' with no replay running", () => {
    render(
      createElement(StatusStrip, {
        status: status({ active: 0, overrides: [] }),
        channels: new Map(),
        events: [],
      }),
    );
    expect(screen.getByText("live")).toBeInTheDocument();
  });

  it("switches to 'replay' while a replay is active", () => {
    render(
      createElement(StatusStrip, {
        status: {
          ...status({ active: 0, overrides: [] }),
          replay: {
            running: true,
            replay_id: "r1",
            source_session_id: "2026-05-17T120000",
            started_at: null,
            speed: 1,
            loop: false,
            output_session_dir: null,
            output_anomalies_path: null,
          },
        },
        channels: new Map(),
        events: [],
      }),
    );
    expect(screen.getByText("replay")).toBeInTheDocument();
  });
});

describe("StatusStrip — ended capture announcement (W03)", () => {
  it("persists an interruption until the operator acknowledges it", () => {
    const interrupted: StatusResponse = {
      ...status({ active: 0, overrides: [] }),
      last_capture: {
        name: "tuning",
        session_dir: "/data/_capture-tuning-2026-09-21T180000",
        started_at: "2026-09-21T18:00:00Z",
        ended_at: "2026-09-21T18:04:00Z",
        end_reason: "interrupted",
        quality_verdict: "unknown",
      },
    };
    render(
      createElement(StatusStrip, {
        status: interrupted,
        channels: new Map(),
        events: [],
      }),
    );
    expect(screen.getByText(/tuning ended after 4 min: daemon restarted/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "acknowledge" }));
    expect(screen.queryByText(/tuning ended after 4 min/)).not.toBeInTheDocument();
  });
});

describe("StatusStrip — accepted-baseline overrides (2.3)", () => {
  it("is not shown when the daemon hasn't wired the field", () => {
    render(
      createElement(StatusStrip, {
        status: null,
        channels: new Map(),
        events: [],
      }),
    );
    expect(screen.queryByText("overrides")).not.toBeInTheDocument();
  });

  it("always shows the active count, even with zero overrides", () => {
    render(
      createElement(StatusStrip, {
        status: status({ active: 0, overrides: [] }),
        channels: new Map(),
        events: [],
      }),
    );
    expect(screen.getByText("overrides")).toBeInTheDocument();
    expect(screen.getByText("0 active")).toBeInTheDocument();
  });

  it("expands to per-override provenance on click", () => {
    render(
      createElement(StatusStrip, {
        status: status({ active: 1, overrides: [OVERRIDE] }),
        channels: new Map(),
        events: [],
      }),
    );
    expect(screen.queryByText(/known weak #3 injector/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("1 active"));
    expect(screen.getByText(/known weak #3 injector/)).toBeInTheDocument();
    expect(screen.getByText(/review by 2026-11-17/)).toBeInTheDocument();
  });

  it("flags an expired override instead of hiding it", () => {
    render(
      createElement(StatusStrip, {
        status: status({
          active: 0,
          overrides: [{ ...OVERRIDE, expired: true }],
        }),
        channels: new Map(),
        events: [],
      }),
    );
    expect(screen.getByText(/expired/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("0 active"));
    expect(screen.getByText("needs re-confirmation")).toBeInTheDocument();
  });
});

function anomaly(overrides: Partial<AnomalyEvent>): AnomalyEvent {
  return {
    timestamp: new Date().toISOString(),
    source: "lstm_autoencoder",
    model_version: "v1",
    score: 5,
    threshold: 3,
    affected_channels: [],
    operating_mode: "city",
    session: null,
    window_start_timestamp: null,
    window_end_timestamp: null,
    latency_ms: null,
    detail: {},
    ...overrides,
  };
}

describe("StatusStrip — detector roles (A06.5)", () => {
  it("shows an alert-role event as an active anomaly", () => {
    render(
      createElement(StatusStrip, {
        status: status({ active: 0, overrides: [] }),
        channels: new Map(),
        events: [anomaly({ role: "alert" })],
      }),
    );
    expect(screen.getByText(/active · LSTM \(city\)/)).toBeInTheDocument();
  });

  it("never shows an evidence-role event as an active anomaly", () => {
    render(
      createElement(StatusStrip, {
        status: status({ active: 0, overrides: [] }),
        channels: new Map(),
        events: [anomaly({ role: "evidence" })],
      }),
    );
    expect(screen.getByText("nominal")).toBeInTheDocument();
  });

  it("reports the freshest alert behind a newer evidence event", () => {
    const now = Date.now();
    render(
      createElement(StatusStrip, {
        status: status({ active: 0, overrides: [] }),
        channels: new Map(),
        events: [
          anomaly({ role: "evidence", timestamp: new Date(now).toISOString() }),
          anomaly({
            role: "alert",
            source: "fused",
            timestamp: new Date(now - 10_000).toISOString(),
          }),
        ],
      }),
    );
    expect(screen.getByText(/active · Fused \(city\)/)).toBeInTheDocument();
  });

  it("treats a record without a role (pre-A06.5) as an alert", () => {
    render(
      createElement(StatusStrip, {
        status: status({ active: 0, overrides: [] }),
        channels: new Map(),
        events: [anomaly({})],
      }),
    );
    expect(screen.getByText(/active · LSTM/)).toBeInTheDocument();
  });
});

import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { AnomalyEvent, StatusResponse } from "../types";
import { ModelEvidencePanel } from "./ModelEvidencePanel";
import { ModelHealthPanel } from "./ModelHealthPanel";

const LSTM_EVENT: AnomalyEvent = {
  timestamp: "2026-05-17T10:00:00+00:00",
  source: "lstm_autoencoder",
  model_version: "lstm_v1",
  score: 0.6,
  threshold: 0.4,
  affected_channels: ["obd:RPM"],
  operating_mode: "idle",
  session: "s1",
  window_start_timestamp: "2026-05-17T09:59:30+00:00",
  window_end_timestamp: "2026-05-17T10:00:00+00:00",
  latency_ms: 12,
  detail: {
    channel_scores: { "obd:RPM": 0.3, "obd:MAF": 0.1 },
    channel_thresholds: { "obd:RPM": 0.2, "obd:MAF": 0.2 },
  },
};

const STATUS: StatusResponse = {
  storage_path: "/tmp/canary",
  free_mb: 2048,
  latest_session: "s1",
  latest_session_path: "/tmp/canary/s1",
  model_versions: { lstm_autoencoder: "lstm_v1" },
  vehicle: "2008 Nissan Xterra",
  emitter_counters: null,
  health: {
    observed: 20,
    inference_latency_ms: { mean: 12, p95: 18, max: 25 },
    missing_feature_rate: 0.05,
    data_freshness_seconds: 1.5,
    model_swaps: { swapped: 1, rejected: 0, unchanged: 3, error: 0 },
    active_model_version: "lstm_v1",
    system: { load_per_cpu: 0.2, mem_used_fraction: 0.4, mem_total_mb: 4096 },
    feature_drift: {
      per_feature_sigma: { "obd:RPM": 3.2, "obd:MAF": 0.4 },
      max_sigma: 3.2,
      drifted_features: ["obd:RPM"],
      missing_features: [],
      schema_mismatch: false,
    },
    reconstruction_error_drift: {
      live_mean_error: 0.12,
      ratio: 1.2,
      sigma_above_train: 0.8,
    },
  },
};

describe("ModelEvidencePanel", () => {
  it("renders LSTM channel reconstruction evidence", () => {
    render(
      createElement(ModelEvidencePanel, {
        event: LSTM_EVENT,
        relatedEvents: [LSTM_EVENT],
      }),
    );
    expect(screen.getByText("Model evidence")).toBeInTheDocument();
    expect(screen.getAllByText("LSTM").length).toBeGreaterThan(0);
    expect(screen.getAllByText("obd:RPM").length).toBeGreaterThan(0);
    expect(screen.getByText("1.50x")).toBeInTheDocument();
  });
});

describe("ModelHealthPanel", () => {
  it("renders status health drift and latency", () => {
    render(
      createElement(ModelHealthPanel, {
        status: STATUS,
        statusError: null,
      }),
    );
    expect(screen.getByText("Drift & Inference Health")).toBeInTheDocument();
    expect(screen.getByText("12.0 ms")).toBeInTheDocument();
    expect(screen.getByText("18.0 ms")).toBeInTheDocument();
    expect(screen.getByText("obd:RPM")).toBeInTheDocument();
    expect(screen.getByText("1.20x")).toBeInTheDocument();
  });
});

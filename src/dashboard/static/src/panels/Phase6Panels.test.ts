import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  DatasetHealthResponse,
  TrainingRunsResponse,
} from "../types";

const mocks = vi.hoisted(() => ({
  getDatasetHealth: vi.fn(),
  getTrainingRuns: vi.fn(),
  submitJob: vi.fn(),
}));

vi.mock("../api/http", () => ({
  getDatasetHealth: mocks.getDatasetHealth,
  getTrainingRuns: mocks.getTrainingRuns,
  submitJob: mocks.submitJob,
  HttpError: class extends Error {
    status = 0;
    body = "";
  },
}));

const { DatasetHealthPanel } = await import("./DatasetHealthPanel");
const { TrainingPanel } = await import("./TrainingPanel");

const COMPANION = {
  baseUrl: "http://127.0.0.1:8765",
  token: "",
  setBaseUrl: vi.fn(),
  setToken: vi.fn(),
};

beforeEach(() => {
  mocks.getDatasetHealth.mockReset();
  mocks.getTrainingRuns.mockReset();
  mocks.submitJob.mockReset();
});
afterEach(() => vi.clearAllMocks());

describe("DatasetHealthPanel", () => {
  it("prompts for a token when none is set (no fetch)", () => {
    render(createElement(DatasetHealthPanel, { token: "" }));
    expect(screen.getByText(/Enter the control token/)).toBeInTheDocument();
    expect(mocks.getDatasetHealth).not.toHaveBeenCalled();
  });

  it("shows the readiness verdict", async () => {
    const resp: DatasetHealthResponse = {
      sessions: [
        {
          session_id: "2026-05-17T120000",
          row_count: 70,
          duration_seconds: 69,
          mode_coverage: { all: 70 },
          tags: [],
          gap_fraction: 0,
          pinned: false,
          training_candidate: true,
          parse_error_ratio: null,
          warnings: [],
        },
      ],
      total_rows: 70,
      total_duration_seconds: 69,
      mode_coverage: { all: 70 },
      channel_availability: { "obd:RPM": 1 },
      readiness: { ready: false, reasons: ["only 1 usable sessions (need ≥ 3)"] },
      warnings: [],
    };
    mocks.getDatasetHealth.mockResolvedValue(resp);
    render(createElement(DatasetHealthPanel, { token: "secret" }));
    expect(await screen.findByText("✗ Not ready to retrain")).toBeInTheDocument();
    expect(
      screen.getByText("only 1 usable sessions (need ≥ 3)"),
    ).toBeInTheDocument();
    expect(screen.getByText("obd:RPM")).toBeInTheDocument();
  });
});

describe("TrainingPanel", () => {
  it("renders a run with its publish status", async () => {
    const resp: TrainingRunsResponse = {
      count: 1,
      runs: [
        {
          run_id: "lstm_v1",
          model_type: "lstm_autoencoder",
          model_version: "lstm_v1",
          created_at: "2026-05-16T10:00:00+00:00",
          status: "succeeded",
          input_sessions: ["s1"],
          feature_schema: ["obd:RPM"],
          hyperparams: {},
          metrics: {
            loss_curve: [0.9, 0.5, 0.3],
            false_positive_benchmark: {
              false_positive_rate: 0.01,
              alerts_per_hour: 0.2,
              meets_v1_budget: true,
              global: {
                held_out_error_distribution: {
                  minimum: 0.01,
                  maximum: 0.4,
                  p50: 0.08,
                  p95: 0.16,
                  threshold: 0.3,
                },
              },
              k_sweep: [
                { k: 2, alerts_per_hour: 1.4, meets_v1_budget: false },
                { k: 3, alerts_per_hour: 0.2, meets_v1_budget: true },
              ],
            },
          },
          artifacts: {},
          publish_status: "active",
          notes: null,
        },
      ],
    };
    mocks.getTrainingRuns.mockResolvedValue(resp);
    render(createElement(TrainingPanel, { token: "secret", companion: COMPANION }));
    expect(await screen.findByText("lstm_v1")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("1 runs")).toBeInTheDocument();
    expect(screen.getByText("budget ok")).toBeInTheDocument();
    expect(screen.getByText("score distribution")).toBeInTheDocument();
  });

  it("starts a laptop training job through the companion API", async () => {
    mocks.getTrainingRuns.mockResolvedValue({ count: 0, runs: [] });
    mocks.submitJob.mockResolvedValue({
      job: {
        job_id: "job1",
        kind: "retrain_if",
        status: "queued",
        created_at: "2026-05-17T10:00:00+00:00",
        started_at: null,
        finished_at: null,
        returncode: null,
        log_path: "job.log",
        command: [],
        params: { days: 14 },
      },
    });
    render(createElement(TrainingPanel, { token: "secret", companion: COMPANION }));

    fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() =>
      expect(mocks.submitJob).toHaveBeenCalledWith(
        "",
        "retrain_if",
        { days: 14 },
        "http://127.0.0.1:8765",
      ),
    );
    expect(await screen.findByText(/queued on laptop/)).toBeInTheDocument();
  });
});

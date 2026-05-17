import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  DatasetHealthResponse,
  ObdCoverageResponse,
  TrainingRunsResponse,
} from "../types";

const mocks = vi.hoisted(() => ({
  getObdCoverage: vi.fn(),
  getDatasetHealth: vi.fn(),
  getTrainingRuns: vi.fn(),
  submitJob: vi.fn(),
}));

vi.mock("../api/http", () => ({
  getObdCoverage: mocks.getObdCoverage,
  getDatasetHealth: mocks.getDatasetHealth,
  getTrainingRuns: mocks.getTrainingRuns,
  submitJob: mocks.submitJob,
  HttpError: class extends Error {
    status = 0;
    body = "";
  },
}));

const { ObdCoveragePanel } = await import("./ObdCoveragePanel");
const { DatasetHealthPanel } = await import("./DatasetHealthPanel");
const { TrainingPanel } = await import("./TrainingPanel");

const COMPANION = {
  baseUrl: "http://127.0.0.1:8765",
  token: "",
  setBaseUrl: vi.fn(),
  setToken: vi.fn(),
};

beforeEach(() => {
  mocks.getObdCoverage.mockReset();
  mocks.getDatasetHealth.mockReset();
  mocks.getTrainingRuns.mockReset();
  mocks.submitJob.mockReset();
});
afterEach(() => vi.clearAllMocks());

describe("ObdCoveragePanel", () => {
  it("renders the coverage summary + a decoded row", async () => {
    const resp: ObdCoverageResponse = {
      supported_count: 2,
      decoded_count: 1,
      supported_not_decoded_count: 1,
      responded_count: 1,
      backed_off_count: 0,
      undecoded_supported: ["MONITOR_STATUS_DRIVE_CYCLE"],
      rows: [
        {
          mode: 1,
          pid: 12,
          pid_hex: "0x0C",
          status: "decoded",
          channel: "RPM",
          display_name: "Engine RPM",
          category: "engine",
          unit: "rpm",
          poll_tier: "fast",
          responded_recently: true,
          backed_off: false,
        },
      ],
    };
    mocks.getObdCoverage.mockResolvedValue(resp);
    render(createElement(ObdCoveragePanel));
    expect(await screen.findByText("Engine RPM")).toBeInTheDocument();
    expect(screen.getByText("1 decoded / 2 supported")).toBeInTheDocument();
    expect(
      screen.getByText(/MONITOR_STATUS_DRIVE_CYCLE/),
    ).toBeInTheDocument();
  });
});

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
          metrics: { loss_curve: [0.9, 0.5, 0.3] },
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

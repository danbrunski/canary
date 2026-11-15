import { createElement } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { mockHttp } from "../test/harness";
import type { AnomalyEvent, JobRecord, ReplayWindowReport } from "../types";

const mocks = mockHttp(["submitJob", "getJob", "getJobResult"]);

const { RescoreControl } = await import("./RescoreControl");

const EVENT: AnomalyEvent = {
  timestamp: "2026-05-17T10:00:00+00:00",
  source: "lstm_autoencoder",
  model_version: "lstm_v1",
  score: 0.6,
  threshold: 0.4,
  affected_channels: ["obd:RPM"],
  operating_mode: "idle",
  session: "2026-05-17T095000",
  window_start_timestamp: "2026-05-17T09:59:30+00:00",
  window_end_timestamp: "2026-05-17T10:00:00+00:00",
  latency_ms: 12,
  detail: {},
};

function job(overrides: Partial<JobRecord> = {}): JobRecord {
  return {
    job_id: "job-1",
    kind: "replay_window",
    status: "running",
    created_at: "2026-07-19T00:00:00Z",
    started_at: "2026-07-19T00:00:01Z",
    finished_at: null,
    returncode: null,
    log_path: "/tmp/job-1.log",
    command: [],
    params: {},
    ...overrides,
  };
}

const REPORT: ReplayWindowReport = {
  session_id: "2026-05-17T095000",
  window: { start: null, end: null, actual_start: null, actual_end: null },
  summary: {
    rows_scored: 30,
    if_anomalies_replayed: 0,
    lstm_windows_scored: 3,
    lstm_anomalies_replayed: 1,
    historical_events: 1,
    matched: 1,
    replayed_only: 0,
    historical_only: 0,
  },
  isolation_forest: {
    loaded: true,
    model_version: "if_v1",
    rows_scored: 30,
    scores: [{ timestamp: "t", operating_mode: "idle", score: 0.1, label: 1 }],
    anomalies: [],
  },
  lstm_autoencoder: {
    loaded: true,
    model_version: "lstm_v1",
    windows_scored: 3,
    windows: [
      {
        timestamp: "t",
        window_start_timestamp: "t0",
        window_end_timestamp: "t1",
        global_score: 0.62,
        global_threshold: 0.4,
        anomalous_channels: ["obd:RPM"],
        channel_scores: {},
        channel_thresholds: {},
        is_anomaly: true,
        latency_ms: 5,
      },
    ],
    anomalies: [
      {
        timestamp: "t",
        window_start_timestamp: "t0",
        window_end_timestamp: "t1",
        global_score: 0.62,
        global_threshold: 0.4,
        anomalous_channels: ["obd:RPM"],
        channel_scores: {},
        channel_thresholds: {},
        is_anomaly: true,
        latency_ms: 5,
      },
    ],
  },
  historical_events: [],
  diff: {},
  warnings: [],
};

describe("RescoreControl (Task 4.1)", () => {
  it("hides the control entirely without a token", () => {
    render(createElement(RescoreControl, { token: "", event: EVENT }));
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("disables the button when the event has no session to replay", () => {
    render(
      createElement(RescoreControl, {
        token: "secret",
        event: { ...EVENT, session: null },
      }),
    );
    expect(screen.getByRole("button", { name: /re-score/ })).toBeDisabled();
  });

  it("submits, polls, and renders the report once the job succeeds", async () => {
    mocks.submitJob.mockResolvedValue({ job: job({ status: "queued" }) });
    mocks.getJob
      .mockResolvedValueOnce({ job: job({ status: "running" }) })
      .mockResolvedValueOnce({ job: job({ status: "succeeded" }) });
    mocks.getJobResult.mockResolvedValue({ job: job({ status: "succeeded" }), result: REPORT });

    render(createElement(RescoreControl, { token: "secret", event: EVENT }));
    fireEvent.click(screen.getByRole("button", { name: /re-score this window/ }));

    expect(mocks.submitJob).toHaveBeenCalledWith("secret", "replay_window", {
      session_id: "2026-05-17T095000",
      start: "2026-05-17T09:59:30+00:00",
      end: "2026-05-17T10:00:00+00:00",
    });

    expect(await screen.findByText("Isolation Forest")).toBeInTheDocument();
    expect(screen.getByText("LSTM autoencoder")).toBeInTheDocument();
    expect(screen.getByText("fired")).toBeInTheDocument();
    expect(screen.getByText(/3 windows scored/)).toBeInTheDocument();
  }, 10000);

  it("renders an honest failed state when the job fails", async () => {
    mocks.submitJob.mockResolvedValue({ job: job({ status: "queued" }) });
    mocks.getJob.mockResolvedValue({ job: job({ status: "failed" }) });

    render(createElement(RescoreControl, { token: "secret", event: EVENT }));
    fireEvent.click(screen.getByRole("button", { name: /re-score this window/ }));

    expect(await screen.findByText(/replay_window job failed/)).toBeInTheDocument();
  }, 10000);

  it("resets when the operator inspects a different event", async () => {
    mocks.submitJob.mockResolvedValue({ job: job({ status: "queued" }) });
    mocks.getJob.mockResolvedValue({ job: job({ status: "succeeded" }) });
    mocks.getJobResult.mockResolvedValue({ job: job({ status: "succeeded" }), result: REPORT });

    const { rerender } = render(createElement(RescoreControl, { token: "secret", event: EVENT }));
    fireEvent.click(screen.getByRole("button", { name: /re-score this window/ }));
    await screen.findByText("Isolation Forest");

    rerender(
      createElement(RescoreControl, {
        token: "secret",
        event: { ...EVENT, timestamp: "2026-05-17T11:00:00+00:00" },
      }),
    );
    expect(screen.queryByText("Isolation Forest")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "re-score this window" })).toBeInTheDocument();
  }, 10000);
});

describe("RescoreControl job poll timing (R28.3)", () => {
  afterEach(() => vi.useRealTimers());

  async function submit() {
    mocks.submitJob.mockResolvedValue({ job: job({ status: "queued" }) });
    const view = render(createElement(RescoreControl, { token: "secret", event: EVENT }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /re-score this window/ }));
      await vi.advanceTimersByTimeAsync(0);
    });
    return view;
  }

  it("first looks 2 s after the submit, then 2 s after each answer settles", async () => {
    vi.useFakeTimers();
    mocks.getJob.mockResolvedValue({ job: job({ status: "running" }) });
    await submit();
    expect(mocks.getJob).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1999);
    });
    expect(mocks.getJob).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(mocks.getJob).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(mocks.getJob).toHaveBeenCalledTimes(2);
  });

  it("never asks again while an answer is outstanding", async () => {
    vi.useFakeTimers();
    mocks.getJob.mockReturnValue(new Promise(() => {}));
    await submit();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(mocks.getJob).toHaveBeenCalledTimes(1);
  });

  it("stops polling once the job is done", async () => {
    vi.useFakeTimers();
    mocks.getJob.mockResolvedValue({ job: job({ status: "succeeded" }) });
    mocks.getJobResult.mockResolvedValue({ job: job({ status: "succeeded" }), result: REPORT });
    await submit();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(screen.getByText("Isolation Forest")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(mocks.getJob).toHaveBeenCalledTimes(1);
  });

  it("shows a failed read as the failure and stops", async () => {
    vi.useFakeTimers();
    mocks.getJob.mockRejectedValue(new Error("daemon went away"));
    await submit();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(screen.getByText("daemon went away")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(mocks.getJob).toHaveBeenCalledTimes(1);
  });

  it("stops polling on unmount", async () => {
    vi.useFakeTimers();
    mocks.getJob.mockResolvedValue({ job: job({ status: "running" }) });
    const { unmount } = await submit();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(mocks.getJob).toHaveBeenCalledTimes(1);
    unmount();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(mocks.getJob).toHaveBeenCalledTimes(1);
  });
});

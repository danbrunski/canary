import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AnomalyEvent, TuningResponse } from "../types";

const mocks = vi.hoisted(() => ({
  getTuning: vi.fn(),
  estimateTuning: vi.fn(),
  updateTuning: vi.fn(),
  revertTuning: vi.fn(),
  getFeedback: vi.fn(),
  postFeedback: vi.fn(),
}));

vi.mock("../api/http", () => ({
  ...mocks,
  HttpError: class extends Error {
    status = 0;
    body = "";
  },
}));

const { TuningPanel } = await import("./TuningPanel");
const { AnomalyPanel } = await import("./AnomalyPanel");

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
});
afterEach(() => vi.clearAllMocks());

const TUNING: TuningResponse = {
  tuning: {
    version: 3,
    threshold_overrides: [{ source: "lstm_autoencoder", multiplier: 2 }],
    suppression_rules: [],
    debounce_policies: [],
  },
};

describe("TuningPanel", () => {
  it("prompts for a token without one", () => {
    render(createElement(TuningPanel, { token: "" }));
    expect(screen.getByText(/Enter the control token/)).toBeInTheDocument();
    expect(mocks.getTuning).not.toHaveBeenCalled();
  });

  it("loads config and runs a before/after estimate", async () => {
    mocks.getTuning.mockResolvedValue(TUNING);
    mocks.estimateTuning.mockResolvedValue({
      before: { evaluated: 5, would_emit: 5, would_suppress: 0 },
      after: { evaluated: 5, would_emit: 3, would_suppress: 2 },
      newly_suppressed: 2,
    });
    render(createElement(TuningPanel, { token: "secret" }));
    expect(await screen.findByText(/v3 ·/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Estimate" }));
    expect(await screen.findByText(/Before \/ after estimate/)).toBeInTheDocument();
    expect(screen.getByText(/\+2 newly muted/)).toBeInTheDocument();
    expect(mocks.estimateTuning).toHaveBeenCalledOnce();
  });
});

const EVENT: AnomalyEvent = {
  timestamp: "2026-05-17T10:00:00+00:00",
  source: "lstm_autoencoder",
  model_version: "lstm_v1",
  score: 0.9,
  threshold: 0.3,
  affected_channels: ["obd:RPM"],
  operating_mode: "idle",
  session: null,
  window_start_timestamp: null,
  window_end_timestamp: null,
  latency_ms: 4,
  detail: {},
};

describe("AnomalyPanel feedback labelling", () => {
  it("posts a verdict when a label button is clicked", async () => {
    mocks.getFeedback.mockResolvedValue({
      feedback: [],
      count: 0,
      summary: {},
    });
    mocks.postFeedback.mockResolvedValue({ feedback: {} });
    render(createElement(AnomalyPanel, { events: [EVENT], token: "secret" }));
    const fp = await screen.findByRole("button", { name: "mark false positive" });
    fireEvent.click(fp);
    await waitFor(() => expect(mocks.postFeedback).toHaveBeenCalledOnce());
    expect(mocks.postFeedback).toHaveBeenCalledWith(
      "secret",
      expect.objectContaining({ model_version: "lstm_v1" }),
      "false_positive",
      null,
    );
  });

  it("has no label buttons without a token", () => {
    render(createElement(AnomalyPanel, { events: [EVENT] }));
    expect(
      screen.queryByRole("button", { name: "mark false positive" }),
    ).not.toBeInTheDocument();
    expect(mocks.getFeedback).not.toHaveBeenCalled();
  });
});

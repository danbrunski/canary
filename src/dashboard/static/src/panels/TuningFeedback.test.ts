import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { AnomalyEvent, TuningResponse } from "../types";
import { mockHttp } from "../test/harness";

const mocks = mockHttp([
  "getTuning",
  "estimateTuning",
  "updateTuning",
  "revertTuning",
  "getFeedback",
  "postFeedback",
]);

const { TuningPanel } = await import("./TuningPanel");
const { AnomalyPanel } = await import("./AnomalyPanel");

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
    // The editor is seeded by an effect *after* the config renders — clicking
    // before it flushes used to parse "" and flake the suite under load
    // (Estimate is also disabled until the draft is non-empty now, so an
    // early click would silently no-op instead of erroring).
    await waitFor(() =>
      expect(screen.getByLabelText("Alert threshold config JSON")).not.toHaveValue(""),
    );
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
      null,
      null,
      null,
    );
  });

  it("has no label buttons without a token", () => {
    render(createElement(AnomalyPanel, { events: [EVENT] }));
    expect(screen.queryByRole("button", { name: "mark false positive" })).not.toBeInTheDocument();
    expect(mocks.getFeedback).not.toHaveBeenCalled();
  });

  it("includes a filled-in hypothesis in the feedback payload", async () => {
    mocks.getFeedback.mockResolvedValue({ feedback: [], count: 0, summary: {} });
    mocks.postFeedback.mockResolvedValue({ feedback: {} });
    render(createElement(AnomalyPanel, { events: [EVENT], token: "secret" }));
    fireEvent.change(await screen.findByLabelText("hypothesis"), {
      target: { value: "possible MAF sensor drift" },
    });
    fireEvent.click(screen.getByRole("button", { name: "mark false positive" }));
    await waitFor(() => expect(mocks.postFeedback).toHaveBeenCalledOnce());
    expect(mocks.postFeedback).toHaveBeenCalledWith(
      "secret",
      expect.objectContaining({ model_version: "lstm_v1" }),
      "false_positive",
      null,
      "possible MAF sensor drift",
      null,
      null,
    );
  });

  it("blocks the verdict and shows a validation message when only one of the clarifying question/answer pair is filled", async () => {
    mocks.getFeedback.mockResolvedValue({ feedback: [], count: 0, summary: {} });
    render(createElement(AnomalyPanel, { events: [EVENT], token: "secret" }));
    fireEvent.change(await screen.findByLabelText("clarifying question"), {
      target: { value: "dirt road or heavy rain?" },
    });
    expect(await screen.findByText(/must both be filled/)).toBeInTheDocument();
    const fp = screen.getByRole("button", { name: "mark false positive" });
    expect(fp).toBeDisabled();
    fireEvent.click(fp);
    expect(mocks.postFeedback).not.toHaveBeenCalled();

    // Filling in the answer too clears the validation state and re-enables submit.
    fireEvent.change(screen.getByLabelText("clarifying answer"), {
      target: { value: "dirt road" },
    });
    expect(screen.queryByText(/must both be filled/)).not.toBeInTheDocument();
    expect(fp).not.toBeDisabled();
  });
});

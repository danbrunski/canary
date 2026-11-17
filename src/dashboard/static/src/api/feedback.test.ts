import { afterEach, describe, expect, it, vi } from "vitest";

import type { AnomalyEvent } from "../types";
import { postFeedback, type FeedbackOptions } from "./http";

afterEach(() => {
  vi.unstubAllGlobals();
});

const EVENT: AnomalyEvent = {
  timestamp: "2026-07-20T10:15:00Z",
  source: "lstm_autoencoder",
  model_version: "lstm_v1",
  score: 0.91,
  threshold: 0.8,
  affected_channels: ["ECM_RPM"],
  operating_mode: "idle",
  session: null,
  window_start_timestamp: null,
  window_end_timestamp: null,
  latency_ms: null,
  detail: {},
};

/** The request `postFeedback` made: the body exactly as it went on the wire. */
async function posted(event: AnomalyEvent, label: string, options?: FeedbackOptions) {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ feedback: {} }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  await postFeedback("secret", event, label, options);
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  return { url, init };
}

describe("postFeedback", () => {
  it("posts the event, the label and the enrichment, each key where it always was", async () => {
    const { url, init } = await posted(EVENT, "false_positive", {
      hypothesis: "possible MAF sensor drift",
      clarifyingQuestion: "dirt road or heavy rain?",
      clarifyingAnswer: "dirt road",
    });
    expect(url).toBe("/control/feedback");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(
      JSON.stringify({
        event: EVENT,
        label: "false_positive",
        reason: null,
        hypothesis: "possible MAF sensor drift",
        clarifying_question: "dirt road or heavy rain?",
        clarifying_answer: "dirt road",
      }),
    );
  });

  it("posts null for every option left out", async () => {
    const { init } = await posted(EVENT, "true_positive");
    expect(JSON.parse(init.body as string)).toEqual({
      event: EVENT,
      label: "true_positive",
      reason: null,
      hypothesis: null,
      clarifying_question: null,
      clarifying_answer: null,
    });
    expect(Object.keys(JSON.parse(init.body as string))).toEqual([
      "event",
      "label",
      "reason",
      "hypothesis",
      "clarifying_question",
      "clarifying_answer",
    ]);
  });

  it("sends the reason when one is given", async () => {
    const { init } = await posted(EVENT, "false_positive", { reason: "sensor swap" });
    expect(JSON.parse(init.body as string).reason).toBe("sensor swap");
  });
});

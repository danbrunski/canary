import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { DifferentialCard, LlmAssistCardsResponse, NarrativeCard } from "../types";
import { mockHttp } from "../test/harness";

const mocks = mockHttp(["getLlmAssistCards"]);

const { LlmAssistPanel } = await import("./LlmAssistPanel");

const DIFFERENTIAL_FIXTURE: DifferentialCard = {
  card_type: "differential",
  generated_at: "2026-07-20T18:00:00+00:00",
  session: "2026-07-20T170000",
  hypotheses: [
    {
      rank: 1,
      label: "possible MAF sensor drift",
      confidence: 0.4,
      supporting_evidence: [
        {
          description: "STFT bank 1 trending +8% at cruise",
          channel: "obd:SHORT_FUEL_TRIM_1",
          session: null,
          timestamp: null,
          detail: null,
        },
      ],
      refuting_evidence: [],
    },
  ],
  framing:
    "These are ranked hypotheses to weigh against the evidence shown, not a diagnosis or a repair instruction.",
  model_name: null,
};

const NARRATIVE_FIXTURE: NarrativeCard = {
  card_type: "narrative",
  generated_at: "2026-07-20T18:00:00+00:00",
  title: "Session summary",
  sections: [{ heading: "Fuel trims", body: "Bank 1 STFT held within 3%." }],
  source_sessions: ["2026-07-20T170000"],
  model_name: null,
};

describe("LlmAssistPanel", () => {
  it("shows a loading state before the first fetch settles", () => {
    mocks.getLlmAssistCards.mockReturnValue(new Promise(() => {}));
    render(createElement(LlmAssistPanel));
    expect(screen.getByText(/checking LLM-assist tier/)).toBeInTheDocument();
  });

  it("shows the explicit absent state when the tier is disabled (the default)", async () => {
    const response: LlmAssistCardsResponse = { enabled: false };
    mocks.getLlmAssistCards.mockResolvedValue(response);
    render(createElement(LlmAssistPanel));
    expect(await screen.findByText(/LLM-assist tier is disabled/)).toBeInTheDocument();
  });

  it("shows a 'no cards yet' placeholder when enabled but empty", async () => {
    const response: LlmAssistCardsResponse = { enabled: true, cards: [] };
    mocks.getLlmAssistCards.mockResolvedValue(response);
    render(createElement(LlmAssistPanel));
    expect(await screen.findByText("No cards yet.")).toBeInTheDocument();
  });

  it("renders a differential card with its ranked hypothesis and framing", async () => {
    const response: LlmAssistCardsResponse = {
      enabled: true,
      cards: [DIFFERENTIAL_FIXTURE],
    };
    mocks.getLlmAssistCards.mockResolvedValue(response);
    render(createElement(LlmAssistPanel));
    expect(await screen.findByText("differential")).toBeInTheDocument();
    expect(screen.getByText("possible MAF sensor drift")).toBeInTheDocument();
    expect(screen.getByText(/Supporting: STFT bank 1 trending/)).toBeInTheDocument();
    expect(screen.getByText(/ranked hypotheses to weigh/)).toBeInTheDocument();
  });

  it("renders a narrative card's sections and source sessions", async () => {
    const response: LlmAssistCardsResponse = {
      enabled: true,
      cards: [NARRATIVE_FIXTURE],
    };
    mocks.getLlmAssistCards.mockResolvedValue(response);
    render(createElement(LlmAssistPanel));
    expect(await screen.findByText("narrative")).toBeInTheDocument();
    expect(screen.getByText("Session summary")).toBeInTheDocument();
    expect(screen.getByText("Bank 1 STFT held within 3%.")).toBeInTheDocument();
    expect(screen.getByText(/Sources: 2026-07-20T170000/)).toBeInTheDocument();
  });

  it("shows an error state when the fetch fails", async () => {
    mocks.getLlmAssistCards.mockRejectedValue(new Error("boom"));
    render(createElement(LlmAssistPanel));
    expect(await screen.findByText("boom")).toBeInTheDocument();
  });
});

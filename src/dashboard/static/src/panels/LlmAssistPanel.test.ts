import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  DifferentialCard,
  LlmAssistCardsResponse,
  NarrativeCard,
  ResolvedEvidence,
} from "../types";
import { mockHttp } from "../test/harness";

const mocks = mockHttp(["getLlmAssistCards"]);

const { LlmAssistPanel } = await import("./LlmAssistPanel");

const EVIDENCE: ResolvedEvidence = {
  id: "ev-abc",
  tool: "trim_trend",
  tool_version: "1",
  subject: "trim_trend:2026-07-20T170000:obd:SHORT_FUEL_TRIM_1:cruise",
  conditions: { regime: "cruise", vehicle_state: null, operating_mode: null },
  valid_from: "2026-07-20T17:00:00Z",
  valid_to: "2026-07-20T17:20:00Z",
  recorded_at: "2026-07-20T18:00:00Z",
  sources: [
    {
      session: "2026-07-20T170000",
      window_start: "2026-07-20T17:00:00Z",
      window_end: "2026-07-20T17:20:00Z",
      channels: ["obd:SHORT_FUEL_TRIM_1"],
    },
  ],
  values: { stft_trend: { value: 8, unit: "%", uncertainty: null } },
  labels: {},
  quality: { n_observations: 1200, timestamp_basis: "acquired", flags: [] },
  result: { status: "evaluated", reason: null, detail: "", residual: null },
  superseded_at: null,
  superseded_by: [],
  retracted_by: null,
};

const DIFFERENTIAL_FIXTURE: DifferentialCard = {
  schema: "llm-assist-card/2",
  card_type: "differential",
  vehicle: "truck",
  generated_at: "2026-07-20T18:00:00+00:00",
  session: "2026-07-20T170000",
  hypotheses: [
    {
      rank: 1,
      label: "possible MAF sensor drift",
      confidence: 0.4,
      supporting_evidence: [
        { evidence_id: "ev-abc", note: "STFT bank one trending rich at cruise", quoted: {} },
      ],
      refuting_evidence: [],
    },
  ],
  framing:
    "These are ranked hypotheses to weigh against the evidence shown, not a diagnosis or a repair instruction.",
  model_name: null,
};

const NARRATIVE_FIXTURE: NarrativeCard = {
  schema: "llm-assist-card/2",
  card_type: "narrative",
  vehicle: "truck",
  generated_at: "2026-07-20T18:00:00+00:00",
  title: "Session summary",
  sections: [
    {
      heading: "Fuel trims",
      body: "Bank one STFT trended rich.",
      evidence: [{ evidence_id: "ev-abc", note: "", quoted: {} }],
    },
  ],
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

  it("renders a differential card with numbers from its resolved evidence", async () => {
    const response: LlmAssistCardsResponse = {
      enabled: true,
      cards: [
        {
          verification: "verified",
          problems: [],
          card: DIFFERENTIAL_FIXTURE,
          evidence: { "ev-abc": EVIDENCE },
        },
      ],
    };
    mocks.getLlmAssistCards.mockResolvedValue(response);
    render(createElement(LlmAssistPanel));
    expect(await screen.findByText("differential")).toBeInTheDocument();
    expect(screen.getByText("possible MAF sensor drift")).toBeInTheDocument();
    expect(screen.getByText(/Supporting: STFT bank one trending/)).toBeInTheDocument();
    expect(screen.getByText(/stft_trend 8.000 %/)).toBeInTheDocument();
    expect(screen.getByText(/ranked hypotheses to weigh/)).toBeInTheDocument();
  });

  it("renders a narrative card's sections, evidence and source sessions", async () => {
    const response: LlmAssistCardsResponse = {
      enabled: true,
      cards: [
        {
          verification: "verified",
          problems: [],
          card: NARRATIVE_FIXTURE,
          evidence: { "ev-abc": EVIDENCE },
        },
      ],
    };
    mocks.getLlmAssistCards.mockResolvedValue(response);
    render(createElement(LlmAssistPanel));
    expect(await screen.findByText("narrative")).toBeInTheDocument();
    expect(screen.getByText("Session summary")).toBeInTheDocument();
    expect(screen.getByText("Bank one STFT trended rich.")).toBeInTheDocument();
    expect(screen.getByText(/Sources: 2026-07-20T170000/)).toBeInTheDocument();
  });

  it("shows a rejected card's problems instead of its claims", async () => {
    const response: LlmAssistCardsResponse = {
      enabled: true,
      cards: [
        {
          verification: "rejected",
          problems: ["'ev-forged' was not minted on 'truck''s memory ledger"],
          card: DIFFERENTIAL_FIXTURE,
          evidence: {},
        },
      ],
    };
    mocks.getLlmAssistCards.mockResolvedValue(response);
    render(createElement(LlmAssistPanel));
    expect(await screen.findByText("rejected")).toBeInTheDocument();
    expect(screen.getByText(/was not minted/)).toBeInTheDocument();
    expect(screen.queryByText("possible MAF sensor drift")).not.toBeInTheDocument();
  });

  it("marks a card whose cited evidence was retracted", async () => {
    const response: LlmAssistCardsResponse = {
      enabled: true,
      cards: [
        {
          verification: "retracted",
          problems: ["ev-abc was retracted by ret-xyz"],
          card: DIFFERENTIAL_FIXTURE,
          evidence: {
            "ev-abc": {
              ...EVIDENCE,
              retracted_by: "ret-xyz",
              superseded_at: "2026-07-21T00:00:00Z",
            },
          },
        },
      ],
    };
    mocks.getLlmAssistCards.mockResolvedValue(response);
    render(createElement(LlmAssistPanel));
    expect(await screen.findByText("retracted evidence")).toBeInTheDocument();
    expect(screen.getByText("ev-abc was retracted by ret-xyz")).toBeInTheDocument();
    expect(screen.getByText(/— retracted/)).toBeInTheDocument();
  });

  it("labels a first-contract card as unverified historical narrative", async () => {
    const response: LlmAssistCardsResponse = {
      enabled: true,
      cards: [
        {
          verification: "unverified_historical",
          problems: [],
          card: {
            card_type: "unverified_historical",
            original_card_type: "differential",
            generated_at: "2026-07-20T18:00:00+00:00",
            notice: "Unverified historical narrative: written before cards cited evidence.",
            original: {},
          },
          evidence: {},
        },
      ],
    };
    mocks.getLlmAssistCards.mockResolvedValue(response);
    render(createElement(LlmAssistPanel));
    expect(await screen.findByText("unverified historical")).toBeInTheDocument();
    expect(screen.getByText(/Unverified historical narrative/)).toBeInTheDocument();
  });

  it("shows an error state when the fetch fails", async () => {
    mocks.getLlmAssistCards.mockRejectedValue(new Error("boom"));
    render(createElement(LlmAssistPanel));
    expect(await screen.findByText("boom")).toBeInTheDocument();
  });
});

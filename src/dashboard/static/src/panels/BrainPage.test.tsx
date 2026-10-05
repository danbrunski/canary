import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  BrainJob,
  BrainStatus,
  EvidenceView,
  InvestigationCardResponse,
  TwinSessionResponse,
} from "../types/brain";

vi.mock("../api/brain", () => ({
  getBrainStatus: vi.fn(),
  getBrainJobs: vi.fn(),
  queryMemory: vi.fn(),
  getInvestigationCard: vi.fn(),
  getTwinSession: vi.fn(),
  submitBrainJob: vi.fn(),
}));

const api = await import("../api/brain");
const { BrainPage, explanationParts, jobStateText } = await import("./BrainPage");

function view(id: string, tool: string, computation?: string): EvidenceView {
  return {
    id,
    tool,
    tool_version: "1",
    subject: `${tool}:s`,
    regime: "all",
    status: computation ? "unevaluable" : "evaluated",
    reason: computation ? "unvalidated" : null,
    detail: computation ? "qualification is blocked by ram_copy_unverified" : "",
    values: [
      { name: "difference_bias", value: 0.4, unit: "rpm", uncertainty: null, record_id: id },
    ],
    residual: null,
    labels: computation ? { computation } : {},
    n_observations: 10,
    standing: "current",
    sources: [],
  };
}

const twin = view("ev-twin", "target_phi_twin", "ecu_command");
const measured = view("ev-measured", "compare_channels");

const cardResponse: InvestigationCardResponse = {
  card: {
    investigation: "inv-1",
    title: "Lean at wide open throttle",
    question: "is the command or the delivery wrong",
    status: "open",
    regime: "all",
    evidence_state: "partially_evaluable",
    calibration: "ZS44A-20261004",
    program_id: "nissan-1ZS44A-uprev-osiris",
    what_happened: [measured],
    hypotheses: [
      {
        label: "the command is as decoded",
        status: "open",
        supporting: [twin],
        refuting: [],
        conflicting: false,
      },
    ],
    unknowns: ["fuel pressure through a pull"],
    next_measurements: ["log fuel pressure through a pull"],
    narrative: null,
    narrative_notice: null,
  },
  parts: { ecu_computation: ["ev-twin"], engine_response: ["ev-measured"] },
};

const job: BrainJob = {
  job_id: "bj-1",
  kind: "twin_session_evaluation",
  params: { session: "_capture-ram-study-2026-10-04T204557" },
  state: "deferred",
  created_at: "2026-10-04T21:00:00+00:00",
  updated_at: "2026-10-04T21:00:00+00:00",
  attempts: 0,
  reason: "collecting:engine_off",
  detail: "waiting for an idle window",
  progress: {},
  result: null,
  started_at: null,
  finished_at: null,
};

const status: BrainStatus = {
  vehicle: "nissan-xterra-2008",
  identity: {
    program_id: "nissan-1ZS44A-uprev-osiris",
    program_status: "known",
    calibration: "ZS44A-20261004",
    image_sha256: null,
  },
  change: "unchanged",
  last_change: { kind: "calibration_changed", at: "2026-10-05T00:00:00+00:00" },
  program_specific_state: { valid: true, valid_since: "x", detail: "" },
  predictions: { calibration: "ZS44A-20261004", valid_since: "x", detail: "" },
  tables: { source: "afr_calibration", reason: null },
  ram_addresses: { status: "unchanged", detail: "a calibration flash rewrites tables" },
  transient: { state: "unknown", reason: "not_reconstructible", detail: "" },
  memory: { records: 3, current: 3 },
  jobs: { deferred: 1 },
};

const twinResponse: TwinSessionResponse = {
  session: "_capture-ram-study-2026-10-04T204557",
  job_id: "bj-1",
  state: "completed",
  job: { ...job, state: "completed", reason: null },
  result: {
    calibration: "ZS44A-20260920",
    program_id: "nissan-1ZS44A-uprev-osiris",
    refusals: [],
    evaluations: [
      {
        slot: 1,
        evidence_id: "ev-twin",
        qualification_blockers: ["ram_copy_unverified"],
        accounting: { candidates: 2, states: { unevaluable: 1, eligible: 1 }, reasons: {} },
        capture_requirement: { reason: "no_eligible_tuples", message: "capture the X trace" },
        missing_inputs: ["flag"],
        trace: [
          {
            t: "2026-10-04T20:47:11Z",
            observed_phi: 1,
            predicted_phi: 1,
            state: "eligible",
            reason: null,
            branch: "ordinary",
            qualified_residual_phi: null,
          },
          {
            t: "2026-10-04T20:47:12Z",
            observed_phi: null,
            predicted_phi: null,
            state: "unevaluable",
            reason: "sentinel",
            branch: null,
            qualified_residual_phi: null,
          },
        ],
        trace_omitted: 0,
      },
    ],
  },
  currency: { program_current: true, program_detail: "computed under the program in force now" },
};

afterEach(() => vi.clearAllMocks());

describe("brain helpers", () => {
  it("keeps ECU computation and measured engine response apart, as the server split them", () => {
    const parts = explanationParts(cardResponse);
    expect(parts.ecu.map((v) => v.id)).toEqual(["ev-twin"]);
    expect(parts.engine.map((v) => v.id)).toEqual(["ev-measured"]);
  });

  it("names why a job waits", () => {
    expect(jobStateText(job)).toBe("deferred (collecting:engine_off)");
    expect(jobStateText({ ...job, state: "queued", reason: null })).toBe("queued");
  });
});

describe("BrainPage", () => {
  it("asks for the token before fetching", () => {
    render(<BrainPage token="" />);
    expect(api.getBrainStatus).not.toHaveBeenCalled();
    expect(screen.getAllByText(/Enter the control token/).length).toBeGreaterThan(0);
  });

  it("explains a discrepancy: card parts, missing inputs, next measurement and the trace", async () => {
    vi.mocked(api.getBrainStatus).mockResolvedValue(status);
    vi.mocked(api.getBrainJobs).mockResolvedValue({ jobs: [job], counts: { deferred: 1 } });
    vi.mocked(api.queryMemory).mockResolvedValue({
      vehicle: "nissan-xterra-2008",
      count: 1,
      records: [
        {
          record: {
            id: "inv-1",
            kind: "investigation",
            title: "Lean at wide open throttle",
            recorded_at: "2026-10-04T21:00:00+00:00",
            status: "open",
          },
          superseded_at: null,
          retracted_by: null,
        },
      ],
    });
    vi.mocked(api.getInvestigationCard).mockResolvedValue(cardResponse);
    vi.mocked(api.getTwinSession).mockResolvedValue(twinResponse);

    render(<BrainPage token="t" />);
    expect(await screen.findByText("nissan-1ZS44A-uprev-osiris")).toBeTruthy();
    expect(screen.getByText("unknown (not_reconstructible)")).toBeTruthy();
    expect(await screen.findByText("deferred (collecting:engine_off)")).toBeTruthy();

    fireEvent.click(await screen.findByText("Lean at wide open throttle"));
    expect(await screen.findByText(/ECU computation/)).toBeTruthy();
    expect(screen.getByText(/Measured engine response/)).toBeTruthy();
    expect(screen.getByText(/unvalidated — qualification is blocked/)).toBeTruthy();
    expect(screen.getByText("log fuel pressure through a pull")).toBeTruthy();
    expect(screen.getByText("fuel pressure through a pull")).toBeTruthy();

    fireEvent.click(screen.getByText("_capture-ram-study-2026-10-04T204557"));
    await waitFor(() => expect(api.getTwinSession).toHaveBeenCalled());
    expect(await screen.findByText(/2 command samples/)).toBeTruthy();
    expect(screen.getByText("flag")).toBeTruthy();
    expect(screen.getByText("capture the X trace")).toBeTruthy();
    expect(screen.getByText(/qualification blocked: ram_copy_unverified/)).toBeTruthy();
  });

  it("queues a session evaluation and reports an existing job by its state", async () => {
    vi.mocked(api.getBrainStatus).mockResolvedValue(status);
    vi.mocked(api.getBrainJobs).mockResolvedValue({ jobs: [], counts: {} });
    vi.mocked(api.queryMemory).mockResolvedValue({ vehicle: "v", count: 0, records: [] });
    vi.mocked(api.submitBrainJob).mockResolvedValue({ job, created: false });
    render(<BrainPage token="t" />);
    fireEvent.change(screen.getByLabelText("session to evaluate"), {
      target: { value: "2026-10-04T204557" },
    });
    fireEvent.click(screen.getByText("Evaluate session"));
    expect(await screen.findByText("already deferred (collecting:engine_off)")).toBeTruthy();
    expect(api.submitBrainJob).toHaveBeenCalledWith("t", "twin_session_evaluation", {
      session: "2026-10-04T204557",
    });
  });

  it("shows the live twin's latest command, its counts and why it is not qualified", async () => {
    vi.mocked(api.getBrainStatus).mockResolvedValue({
      ...status,
      live_twin: {
        enabled: true,
        result: "no_eligible_samples",
        reason: null,
        session: "_capture-x",
        replay: false,
        vehicle_state: "engine_running",
        applicability: { qualified: false, qualification_blockers: ["ram_copy_unverified"] },
        accounting: { candidates: 3, states: { unevaluable: 2, eligible: 1 } },
        stream: { pending: 4, late_reads: { "mem:A": 1 } },
        intake: { dropped: 0, busy_ticks: 0 },
        latest_prediction: {
          acquired_at: "2026-10-04T20:47:11Z",
          observed_phi: 1.1640625,
          predicted_phi: 1.1640625,
          difference_phi: 0,
          qualified_residual_phi: null,
          state: "eligible",
          reason: null,
          branch: "override",
        },
      },
    });
    vi.mocked(api.getBrainJobs).mockResolvedValue({ jobs: [], counts: {} });
    vi.mocked(api.queryMemory).mockResolvedValue({ vehicle: "v", count: 0, records: [] });
    render(<BrainPage token="t" />);
    expect(await screen.findByText(/vehicle engine_running/)).toBeTruthy();
    expect(screen.getByText(/predicted φ 1.16406 .* residual not qualified/)).toBeTruthy();
    expect(screen.getByText(/pending 4 · late 1 · dropped 0/)).toBeTruthy();
    expect(screen.getByText("ram_copy_unverified")).toBeTruthy();
  });
});

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlJson } from "../../api/core";
import type { TuningContext } from "../../hooks/useTuningContext";
import type {
  Conditions,
  DeliveryErrorResponse,
  LogsResponse,
  PullComparisonResponse,
  TrimSurfaceResponse,
} from "../../types/workbench";
import type { StatusResponse } from "../../types";
import { AnalysisPage } from "./AnalysisPage";

vi.mock("../../api/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/core")>();
  return { ...actual, controlJson: vi.fn() };
});
const control = vi.mocked(controlJson);

const LOGS: LogsResponse = {
  include_sessions: false,
  total: 0,
  limit: 100,
  capped: false,
  logs: [],
};

const CONDITIONS: Conditions = {
  sessions: ["2026-09-18T120000", "2026-09-19T120000"],
  n_sessions: 2,
  revisions: { "ZS44A-20260920": ["2026-09-18T120000", "2026-09-19T120000"] },
  mixed_revisions: false,
  revision_boundaries: {},
  baro: { spec: "obd:BAROMETRIC_PRESSURE", min_kpa: 63.1, max_kpa: 80.4, sessions_without: [] },
  gates: { started: 120, filtered: { closed_loop: 90, fuel_cut: 10 }, remaining: 20 },
};

const FLOOR = {
  available: true,
  channel: "delivery_error_pct",
  unit: "percent",
  sd: 1.49,
  mean: -0.36,
  n_cells: 5,
  min: -2.38,
  max: 1.38,
  conditions: "WOT: throttle>=85, 3000-5000 rpm",
};

function delivery(overrides: Partial<DeliveryErrorResponse> = {}): DeliveryErrorResponse {
  return {
    available: true,
    unavailable_kind: null,
    measured: "obd:O2_WR_LAMBDA_S1",
    min_load: null,
    n: 20,
    band_averaged_pct: 1.9,
    pooled_pct: 2.1,
    engine_version: 2,
    pairing: {
      method: "acquisition-time match_ordered",
      max_separation_s: 0.25,
      target_source: "mode01_pid_0x44",
      target_spec: "obd:COMMANDED_EQUIV_RATIO",
      measured_source: "ecm_lambda_pid_0x24",
      measured_spec: "obd:O2_WR_LAMBDA_S1",
      regime: "enrichment",
      steady_target_dwell_s: 0.6,
      target_step_tolerance: 0.01,
      post_fuel_cut_holdoff_s: 6,
      transport_delay_modelled: false,
      transport_delay_s: null,
      transport_delay_bracket_s: null,
      transport_delay_note: "transport delay is not modelled on this route",
      constants_informed_by: ["_capture-tuning-2026-09-21T232153"],
    },
    delay_sensitivity: null,
    bands: [
      {
        rpm_band: "(2500.0, 3000.0]",
        n: 12,
        commanded: 12.5,
        measured: 12.7,
        error_pct: 2.0,
        p05_pct: 0.5,
        p95_pct: 3.5,
      },
      {
        rpm_band: "(3000.0, 3500.0]",
        n: 8,
        commanded: 12.4,
        measured: 12.6,
        error_pct: 1.8,
        p05_pct: 0.4,
        p95_pct: 3.1,
      },
    ],
    per_session: [],
    gates: CONDITIONS.gates,
    unavailable: [],
    noise_floor: FLOOR,
    conditions: CONDITIONS,
    ...overrides,
  };
}

const RUNNING = { registry_revision: "ZS44A-20260920" } as StatusResponse;

function context(): TuningContext {
  return {
    selectedLogId: null,
    setSelectedLogId: () => {},
    loadedRom: null,
    setLoadedRom: () => {},
  };
}

function route(handlers: Record<string, unknown>) {
  control.mockImplementation((path: string) => {
    if (path.startsWith("/control/logs")) return Promise.resolve(LOGS);
    for (const [prefix, value] of Object.entries(handlers)) {
      if (path.startsWith(prefix)) {
        return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
      }
    }
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}

afterEach(() => vi.clearAllMocks());

async function chooseRunningAndSource(source = "obd:O2_WR_LAMBDA_S1") {
  fireEvent.click(screen.getByRole("button", { name: /All on the running calibration/ }));
  fireEvent.change(screen.getByLabelText("Measured-AFR source"), { target: { value: source } });
  fireEvent.click(screen.getByRole("button", { name: "Run" }));
}

describe("AnalysisPage", () => {
  it("with no sessions chosen asks for them and offers the running calibration", () => {
    route({});
    render(<AnalysisPage token="t" status={RUNNING} context={context()} />);
    expect(screen.getByText(/Choose sessions or a revision/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "All on the running calibration (ZS44A-20260920)" }),
    ).toBeEnabled();
  });

  it("never picks a measured-AFR source for the operator", () => {
    route({});
    render(<AnalysisPage token="t" status={RUNNING} context={context()} />);
    fireEvent.click(screen.getByRole("button", { name: /All on the running calibration/ }));
    expect(screen.getByLabelText("Measured-AFR source")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
  });

  it("draws the noise floor, n on every bar, and the conditions", async () => {
    route({ "/analysis/delivery-error": delivery() });
    const { container } = render(<AnalysisPage token="t" status={RUNNING} context={context()} />);
    await chooseRunningAndSource();
    expect(await screen.findByLabelText("Delivery error by rpm band")).toBeInTheDocument();
    expect(control).toHaveBeenCalledWith(
      "/analysis/delivery-error?revision=ZS44A-20260920&measured=obd%3AO2_WR_LAMBDA_S1",
      "t",
      expect.anything(),
    );
    expect(screen.getByText("2.00% · n 12")).toBeInTheDocument();
    expect(screen.getByText("1.80% · n 8")).toBeInTheDocument();
    expect(container.querySelector('[data-floor="sd"]')).not.toBeNull();
    expect(container.querySelector('[data-floor="range"]')).not.toBeNull();
    expect(screen.getByText(/Measured under: WOT: throttle>=85/)).toBeInTheDocument();
    // R05.16: the pairing the numbers rest on is stated, not implied.
    expect(screen.getByRole("note", { name: "Pairing" })).toHaveTextContent(
      "within 250 ms; transport delay is not modelled on this route; " +
        "only a target steady within 0.01 λ for 0.6 s and 6 s clear of fuel cut counts",
    );
    expect(screen.queryByRole("alert", { name: "Delay sensitivity" })).toBeNull();
    const conditions = screen.getByLabelText("Conditions");
    expect(conditions).toHaveTextContent("2 pooled");
    expect(conditions).toHaveTextContent("63.1–80.4 kPa");
    expect(conditions).toHaveTextContent(
      "20 of 120 samples passed: closed loop removed 90, fuel cut removed 10",
    );
  });

  it("states a modelled delay and flags a result that does not survive its bracket", async () => {
    const base = delivery();
    route({
      "/analysis/delivery-error": delivery({
        pairing: {
          ...base.pairing,
          target_source: "ram_final_target",
          target_spec: "mem:ECM_TARGET_EQUIV_RATIO",
          transport_delay_modelled: true,
          transport_delay_s: 0.1,
          transport_delay_bracket_s: [0, 0.5],
        },
        delay_sensitivity: {
          at: [
            { delay_s: 0.1, n: 20, band_averaged_pct: 0.7, pooled_pct: 0.6 },
            { delay_s: 0, n: 22, band_averaged_pct: -0.3, pooled_pct: -0.2 },
            { delay_s: 0.5, n: 18, band_averaged_pct: 1.1, pooled_pct: 1.0 },
          ],
          stable: false,
          reason: "the band-averaged sign changes across the delay bracket",
          spread_pct: 1.4,
        },
      }),
    });
    render(<AnalysisPage token="t" status={RUNNING} context={context()} />);
    await chooseRunningAndSource();
    expect(await screen.findByRole("note", { name: "Pairing" })).toHaveTextContent(
      "with the λ read nearest t + 100 ms (transport delay, 0–0.5 s supported)",
    );
    expect(screen.getByRole("alert", { name: "Delay sensitivity" })).toHaveTextContent(
      "the band-averaged sign changes across the delay bracket (0.1 s: 0.70 %, 0 s: -0.30 %, 0.5 s: 1.10 %)",
    );
  });

  it("keeps the two kinds of empty distinguishable", async () => {
    route({
      "/analysis/delivery-error": delivery({
        available: false,
        unavailable_kind: "gates_removed_everything",
        n: 0,
        bands: [],
        gates: { started: 9, filtered: { fuel_cut: 9 }, remaining: 0 },
      }),
    });
    const first = render(<AnalysisPage token="t" status={RUNNING} context={context()} />);
    await chooseRunningAndSource();
    const gates = await screen.findByText("0 of 9 samples passed: fuel cut removed 9");
    expect(gates).toHaveAttribute("data-empty", "gates");
    first.unmount();

    route({
      "/analysis/delivery-error": delivery({
        available: false,
        unavailable_kind: "not_captured",
        n: 0,
        bands: [],
        gates: null,
        unavailable: [
          {
            session_id: "s",
            kind: "not_captured",
            reason: "never captured",
            missing: ["obd:COMMANDED_EQUIV_RATIO"],
          },
        ],
      }),
    });
    render(<AnalysisPage token="t" status={RUNNING} context={context()} />);
    await chooseRunningAndSource();
    const missing = await screen.findByText(
      "This capture's profile does not poll obd:COMMANDED_EQUIV_RATIO",
    );
    expect(missing).toHaveAttribute("data-empty", "not-captured");
  });

  it("says deferred while a capture records", async () => {
    route({
      "/analysis/delivery-error": new HttpError(
        409,
        JSON.stringify({
          detail: {
            error: "analysis_deferred",
            profile: "tuning",
            message: "analysis is deferred while 'tuning' is recording",
            laptop_path: "ssh … tar -cf - …",
          },
        }),
      ),
    });
    render(<AnalysisPage token="t" status={RUNNING} context={context()} />);
    await chooseRunningAndSource();
    expect(await screen.findByText(/is recording/)).toHaveTextContent(
      "Deferred while tuning is recording",
    );
  });

  it("greys sparse trim cells and splits per session", async () => {
    const cell = (total: number, sparse: boolean, n: number) => ({
      rpm_index: 3,
      airflow_index: 4,
      rpm_range: [2000, 2500] as [number, number],
      airflow_range: [15, 20] as [number, number],
      n,
      sparse,
      total,
      stft: 0,
      ltft: total,
    });
    const trim: TrimSurfaceResponse = {
      available: true,
      unavailable_kind: null,
      value: "total",
      axes: {
        rpm: "obd:RPM",
        airflow: "obd:MAF",
        rpm_edges: [500, 1000, 1500, 2000, 2500],
        airflow_edges: [0, 3, 6, 10, 15, 20],
      },
      min_n: 5,
      n: 40,
      banks: { b1: { n: 40, cells: [cell(-5, false, 40)] } },
      per_session: [
        { session_id: "2026-09-18T120000", revision: "R", cells: { b1: [cell(-5, true, 2)] } },
      ],
      gates: { started: 60, filtered: { not_closed_loop: 20 }, remaining: 40 },
      unavailable: [],
      conditions: CONDITIONS,
    };
    route({ "/analysis/trim-surface": trim });
    const { container } = render(<AnalysisPage token="t" status={RUNNING} context={context()} />);
    fireEvent.click(screen.getByRole("button", { name: /All on the running calibration/ }));
    fireEvent.click(screen.getByRole("button", { name: "Trim surface" }));
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByRole("table", { name: "Trim surface" })).toBeInTheDocument();
    expect(container.querySelector('[data-sparse="true"]')).toBeNull();
    fireEvent.change(screen.getByLabelText("Split"), { target: { value: "2026-09-18T120000" } });
    await waitFor(() => expect(container.querySelector('[data-sparse="true"]')).not.toBeNull());
  });

  it("outlines a timing drop between revisions", async () => {
    const compare: PullComparisonResponse = {
      available: true,
      unavailable_kind: null,
      value: "obd:TIMING_ADVANCE",
      revision_a: "A",
      revision_b: "B",
      axes: {
        rpm: "obd:RPM",
        airflow: "obd:MAF",
        rpm_edges: [3000, 3500, 4000],
        airflow_edges: [100, 130, 170],
      },
      min_n: 5,
      cells: [
        {
          rpm_index: 0,
          airflow_index: 0,
          rpm_range: [3000, 3500],
          airflow_range: [100, 130],
          n_a: 9,
          n_b: 9,
          mean_a: 24,
          mean_b: 20,
          delta: -4,
          dropped: true,
          delta_sigma: null,
        },
        {
          rpm_index: 1,
          airflow_index: 1,
          rpm_range: [3500, 4000],
          airflow_range: [130, 170],
          n_a: 9,
          n_b: 9,
          mean_a: 22,
          mean_b: 23,
          delta: 1,
          dropped: false,
          delta_sigma: null,
        },
      ],
      n_cells: 2,
      gates: { started: 40, filtered: {}, remaining: 36 },
      noise_floor: null,
      unavailable: [],
      conditions: CONDITIONS,
    };
    route({
      "/control/calibration": { history: [{ revision: "A" }, { revision: "B" }] },
      "/analysis/pull-comparison": compare,
    });
    const { container } = render(<AnalysisPage token="t" status={RUNNING} context={context()} />);
    fireEvent.click(screen.getByRole("button", { name: "Timing" }));
    fireEvent.click(screen.getByRole("button", { name: "Compare revisions" }));
    await screen.findAllByRole("option", { name: "B" });
    fireEvent.change(screen.getByLabelText("Revision A"), { target: { value: "A" } });
    fireEvent.change(screen.getByLabelText("Revision B"), { target: { value: "B" } });
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(await screen.findByRole("table", { name: "Timing comparison" })).toBeInTheDocument();
    expect(container.querySelectorAll('[data-flagged="true"]').length).toBe(1);
    expect(screen.getByText(/1 cell\(s\) outlined/)).toBeInTheDocument();
  });

  it("names a refused noise floor instead of drawing its band (R05.2)", async () => {
    const compare: PullComparisonResponse = {
      available: true,
      unavailable_kind: null,
      value: "obd:TIMING_ADVANCE",
      revision_a: "A",
      revision_b: "B",
      axes: {
        rpm: "obd:RPM",
        airflow: "obd:MAF",
        rpm_edges: [3000, 3500],
        airflow_edges: [100, 130],
      },
      min_n: 5,
      cells: [
        {
          rpm_index: 0,
          airflow_index: 0,
          rpm_range: [3000, 3500],
          airflow_range: [100, 130],
          n_a: 9,
          n_b: 9,
          mean_a: 24,
          mean_b: 20,
          delta: -4,
          dropped: true,
          delta_sigma: null,
        },
      ],
      n_cells: 1,
      gates: { started: 18, filtered: {}, remaining: 18 },
      noise_floor: { ...FLOOR, channel: "timing_advance_deg", unit: "deg", sd: 0.48 },
      significance: {
        available: false,
        channel: "timing_advance_deg",
        kind: "conditions_mismatch",
        reason: "mismatch",
        floor_conditions: "WOT: throttle>=85, 3000-5000 rpm",
        requested_conditions: "all conditions: every row, binned on rpm x airflow",
      },
      unavailable: [],
      conditions: CONDITIONS,
    };
    route({
      "/control/calibration": { history: [{ revision: "A" }, { revision: "B" }] },
      "/analysis/pull-comparison": compare,
    });
    render(<AnalysisPage token="t" status={RUNNING} context={context()} />);
    fireEvent.click(screen.getByRole("button", { name: "Timing" }));
    fireEvent.click(screen.getByRole("button", { name: "Compare revisions" }));
    await screen.findAllByRole("option", { name: "B" });
    fireEvent.change(screen.getByLabelText("Revision A"), { target: { value: "A" } });
    fireEvent.change(screen.getByLabelText("Revision B"), { target: { value: "B" } });
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    const note = await screen.findByRole("note", { name: "Noise floor refused" });
    expect(note).toHaveTextContent("WOT: throttle>=85, 3000-5000 rpm");
    expect(note).toHaveTextContent("all conditions: every row, binned on rpm x airflow");
    expect(screen.queryByText(/Noise floor \(null run\)/)).not.toBeInTheDocument();
  });
});

describe("AnalysisPage request state (R08.2)", () => {
  function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (err: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }

  const WITH_SESSION: LogsResponse = {
    ...LOGS,
    include_sessions: true,
    total: 1,
    logs: [
      {
        session_id: "2026-09-19T120000",
        is_capture: false,
        profile: null,
        started_at: null,
        duration_s: null,
        duration_source: null,
        row_count: null,
        revision: { revision: "R", source: "registry", conflict: false },
        revision_boundaries: [],
        quality: null,
        end_reason: null,
        tag_count: 0,
      },
    ],
  };

  it("drops a stale logs reply that lands after the toggle", async () => {
    const first = deferred<LogsResponse>();
    control.mockImplementation((path: string) => {
      if (path === "/control/logs?include_sessions=false") return first.promise;
      if (path === "/control/logs?include_sessions=true") return Promise.resolve(WITH_SESSION);
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    render(<AnalysisPage token="t" status={RUNNING} context={context()} />);
    fireEvent.click(screen.getByLabelText("Include ordinary sessions"));
    expect(await screen.findByText("2026-09-19T120000")).toBeInTheDocument();
    first.reject(new HttpError(500, "boom"));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText(/Server error|HTTP 500/)).not.toBeInTheDocument();
    expect(screen.getByText("2026-09-19T120000")).toBeInTheDocument();
  });

  it("clears a logs refusal once the next listing succeeds", async () => {
    control.mockImplementation((path: string) => {
      if (path === "/control/logs?include_sessions=false")
        return Promise.reject(new HttpError(500, "boom"));
      if (path === "/control/logs?include_sessions=true") return Promise.resolve(WITH_SESSION);
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    render(<AnalysisPage token="t" status={RUNNING} context={context()} />);
    expect(await screen.findByText("Server error 500.")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Include ordinary sessions"));
    expect(await screen.findByText("2026-09-19T120000")).toBeInTheDocument();
    expect(screen.queryByText("Server error 500.")).not.toBeInTheDocument();
  });

  it("keeps a Compare refusal out of the Cells view", async () => {
    route({
      "/control/calibration": { history: [{ revision: "A" }, { revision: "B" }] },
      "/analysis/pull-comparison": new HttpError(
        409,
        JSON.stringify({ detail: { error: "analysis_busy", message: "comparison busy" } }),
      ),
    });
    render(<AnalysisPage token="t" status={RUNNING} context={context()} />);
    fireEvent.click(screen.getByRole("button", { name: "Timing" }));
    fireEvent.click(screen.getByRole("button", { name: "Compare revisions" }));
    await screen.findAllByRole("option", { name: "B" });
    fireEvent.change(screen.getByLabelText("Revision A"), { target: { value: "A" } });
    fireEvent.change(screen.getByLabelText("Revision B"), { target: { value: "B" } });
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(await screen.findByText("comparison busy")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cells" }));
    expect(screen.queryByText("comparison busy")).not.toBeInTheDocument();
    // And back: the refusal belongs to Compare and is still there.
    fireEvent.click(screen.getByRole("button", { name: "Compare revisions" }));
    expect(screen.getByText("comparison busy")).toBeInTheDocument();
  });

  it("says the daemon is unreachable in the page's shared words", async () => {
    const { NetworkError } = await import("../../api/core");
    route({
      "/analysis/delivery-error": new NetworkError("/analysis/delivery-error", new TypeError("x")),
    });
    render(<AnalysisPage token="t" status={RUNNING} context={context()} />);
    await chooseRunningAndSource();
    expect(await screen.findByText(/Can't reach the daemon/)).toBeInTheDocument();
  });
});

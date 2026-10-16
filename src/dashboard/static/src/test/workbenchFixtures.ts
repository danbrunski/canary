// Fixtures shared by the Logs and Analysis page tests and by the tests of the
// views composed into them (R19). Each builder returns a complete, valid
// response with the smallest content a test needs; a test overrides the one
// field it is about.

import type { TuningContext } from "../hooks/useTuningContext";
import type { StatusResponse } from "../types";
import type {
  Cell,
  Conditions,
  DeliveryErrorResponse,
  FrameResponse,
  LogSummary,
  LogsResponse,
  NoiseFloor,
  PullComparisonResponse,
  TimingResponse,
  TrimSurfaceResponse,
} from "../types/workbench";

// ---------------------------------------------------------------- logs

export const CAPTURE_ID = "_capture-tuning-2026-09-20T120000";

export const LOGS: LogsResponse = {
  include_sessions: false,
  total: 2,
  limit: 100,
  capped: false,
  logs: [
    {
      session_id: CAPTURE_ID,
      is_capture: true,
      profile: "tuning",
      started_at: "2026-09-20T12:00:00+00:00",
      duration_s: 754,
      duration_source: "capture_manifest",
      row_count: 123456,
      revision: { revision: "ZS44A-20260920", source: "capture_manifest", conflict: false },
      revision_boundaries: [],
      quality: { verdict: "pass", scope: "stop" },
      end_reason: "operator_stop",
      tag_count: 2,
    },
    {
      session_id: "_capture-tuning-2026-09-10T120000",
      is_capture: true,
      profile: "tuning-did",
      started_at: "2026-09-10T12:00:00+00:00",
      duration_s: 60,
      duration_source: "parquet_span",
      row_count: 900,
      revision: { revision: "ZS44A-20260727", source: "registry", conflict: false },
      revision_boundaries: [
        { revision: "ZS44A-20260910", flashed_at: "2026-09-10T12:00:30+00:00" },
      ],
      quality: null,
      end_reason: "daemon_shutdown",
      tag_count: 0,
    },
  ],
};

/** An ordinary (non-capture) session: every optional field absent. */
export function ordinarySession(sessionId = "2026-09-19T120000"): LogSummary {
  return {
    session_id: sessionId,
    is_capture: false,
    profile: null,
    started_at: null,
    duration_s: null,
    duration_source: null,
    row_count: null,
    revision: { revision: null, source: "unknown", conflict: false },
    revision_boundaries: [],
    quality: null,
    end_reason: null,
    tag_count: 0,
  };
}

export function logsResponse(overrides: Partial<LogsResponse> = {}): LogsResponse {
  return { ...LOGS, ...overrides };
}

export function frame(overrides: Partial<FrameResponse> = {}): FrameResponse {
  return {
    session_id: CAPTURE_ID,
    profile: "tuning",
    grid: "1s",
    columns: [
      { spec: "obd:RPM", unit: "rpm" },
      { spec: "obd:MAF", unit: "g/s" },
    ],
    missing: [],
    timestamps: [
      "2026-09-20T12:00:00+00:00",
      "2026-09-20T12:00:01+00:00",
      "2026-09-20T12:00:02+00:00",
    ],
    values: { "obd:RPM": [800, 3000, 1500], "obd:MAF": [4, 60, 20] },
    regime_flags: {},
    regimes: {},
    regime_report: { regime: "all", before: 3, after: 3 },
    freshness: { cells_with_any: 5, kept: 3, dropped: 2, rule: "every channel" },
    rows_total: 3,
    offset: 0,
    rows_returned: 3,
    row_cap: 20000,
    capped: false,
    tags: [{ t: "2026-09-20T12:00:01+00:00", label: "pull_start" }],
    revision: { revision: "ZS44A-20260920", source: "capture_manifest", conflict: false },
    revision_boundaries: [],
    span: { first: null, last: null },
    ...overrides,
  };
}

/** `rows` one-second rows of two channels (rpm 1000 + 10 i, maf i mod 50), so a
 *  virtualized table has something to window and every row is identifiable. */
export function longFrame(rows: number, overrides: Partial<FrameResponse> = {}): FrameResponse {
  const start = Date.parse("2026-09-20T12:00:00+00:00");
  return frame({
    timestamps: Array.from({ length: rows }, (_, i) => new Date(start + i * 1000).toISOString()),
    values: {
      "obd:RPM": Array.from({ length: rows }, (_, i) => 1000 + i * 10),
      "obd:MAF": Array.from({ length: rows }, (_, i) => i % 50),
    },
    rows_total: rows,
    rows_returned: rows,
    tags: [],
    ...overrides,
  });
}

export function tuningContext(overrides: Partial<TuningContext> = {}): TuningContext {
  return {
    selectedLogId: null,
    setSelectedLogId: () => {},
    loadedRom: null,
    setLoadedRom: () => {},
    ...overrides,
  };
}

// ---------------------------------------------------------------- analysis

export const RUNNING = { registry_revision: "ZS44A-20260920" } as StatusResponse;

export const CONDITIONS: Conditions = {
  sessions: ["2026-09-18T120000", "2026-09-19T120000"],
  n_sessions: 2,
  revisions: { "ZS44A-20260920": ["2026-09-18T120000", "2026-09-19T120000"] },
  mixed_revisions: false,
  revision_boundaries: {},
  baro: { spec: "obd:BAROMETRIC_PRESSURE", min_kpa: 63.1, max_kpa: 80.4, sessions_without: [] },
  gates: { started: 120, filtered: { closed_loop: 90, fuel_cut: 10 }, remaining: 20 },
};

export const FLOOR: NoiseFloor = {
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

export function delivery(overrides: Partial<DeliveryErrorResponse> = {}): DeliveryErrorResponse {
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

export function trimCell(total: number, sparse: boolean, n: number): Cell {
  return {
    rpm_index: 3,
    airflow_index: 4,
    rpm_range: [2000, 2500],
    airflow_range: [15, 20],
    n,
    sparse,
    total,
    stft: 0,
    ltft: total,
  };
}

export function trimSurface(overrides: Partial<TrimSurfaceResponse> = {}): TrimSurfaceResponse {
  return {
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
    banks: { b1: { n: 40, cells: [trimCell(-5, false, 40)] } },
    per_session: [
      {
        session_id: "2026-09-18T120000",
        revision: "R",
        cells: { b1: [trimCell(-5, true, 2)] },
      },
    ],
    gates: { started: 60, filtered: { not_closed_loop: 20 }, remaining: 40 },
    unavailable: [],
    conditions: CONDITIONS,
    ...overrides,
  };
}

export function timingResponse(overrides: Partial<TimingResponse> = {}): TimingResponse {
  return {
    available: true,
    unavailable_kind: null,
    value: "obd:TIMING_ADVANCE",
    axes: {
      rpm: "obd:RPM",
      airflow: "obd:MAF",
      rpm_edges: [3000, 3500, 4000],
      airflow_edges: [100, 130, 170],
    },
    min_n: 5,
    n: 30,
    cells: [
      {
        rpm_index: 0,
        airflow_index: 0,
        rpm_range: [3000, 3500],
        airflow_range: [100, 130],
        n: 12,
        sparse: false,
        timing: 24.5,
      },
      {
        rpm_index: 1,
        airflow_index: 1,
        rpm_range: [3500, 4000],
        airflow_range: [130, 170],
        n: 3,
        sparse: true,
        timing: 22,
      },
    ],
    gates: { started: 40, filtered: {}, remaining: 36 },
    unavailable: [],
    conditions: CONDITIONS,
    ...overrides,
  };
}

/** Two compared cells: the first dropped timing (A advanced, B did not). */
export function pullComparison(
  overrides: Partial<PullComparisonResponse> = {},
): PullComparisonResponse {
  return {
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
        n_b: 7,
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
    ...overrides,
  };
}

/** A promise the test settles by hand, for in-flight request ordering. */
export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

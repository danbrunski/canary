import { HttpError, controlJson } from "../api/core";

/** One row of the capture browser (`GET /control/logs`, W04.1). */
export interface LogSummary {
  session_id: string;
  is_capture: boolean;
  profile: string | null;
  started_at: string | null;
  duration_s: number | null;
  duration_source: "capture_manifest" | "parquet_span" | null;
  row_count: number | null;
  revision: { revision: string | null; source: string; conflict: boolean };
  revision_boundaries: RevisionBoundary[];
  quality: { verdict: string | null; scope: string | null } | null;
  end_reason: string | null;
  tag_count: number;
}

export interface LogsResponse {
  include_sessions: boolean;
  total: number;
  limit: number;
  capped: boolean;
  logs: LogSummary[];
}

export interface RevisionBoundary {
  revision: string;
  flashed_at: string;
}

export type Regime = "all" | "closed_loop" | "enrichment" | "fuel_cut" | "idle";
export const REGIMES: ReadonlyArray<{ id: Regime; label: string }> = [
  { id: "all", label: "All" },
  { id: "closed_loop", label: "Closed loop" },
  { id: "enrichment", label: "Enrichment" },
  { id: "fuel_cut", label: "Fuel cut" },
  { id: "idle", label: "Idle" },
];

/** One log as a wide frame (`GET /control/sessions/{id}/frame`, W04.3). */
export interface FrameResponse {
  session_id: string;
  profile: string | null;
  grid: string;
  columns: Array<{ spec: string; unit: string | null }>;
  missing: Array<{ spec: string; reason: string }>;
  timestamps: string[];
  values: Record<string, Array<number | null>>;
  regime_flags: Record<string, Array<boolean | null>>;
  regimes: Record<string, { available: boolean; reason?: string; source?: string }>;
  regime_report: {
    regime: Regime;
    before: number;
    after: number;
    available?: boolean;
    reason?: string;
    removed?: number;
    unknown?: number;
  };
  freshness: { cells_with_any: number; kept: number; dropped: number; rule: string };
  rows_total: number;
  offset: number;
  rows_returned: number;
  row_cap: number;
  capped: boolean;
  tags: Array<{ t: string; label: string }>;
  revision: { revision: string | null; source: string; conflict: boolean };
  revision_boundaries: RevisionBoundary[];
  span: { first: string | null; last: string | null };
}

export interface GateReport {
  started: number;
  filtered: Record<string, number>;
  remaining: number;
  rule?: string;
}

export interface UnavailableSession {
  session_id: string;
  kind: "not_captured" | "refused" | "over_budget";
  reason: string;
  missing: string[];
}

export interface Conditions {
  sessions: string[];
  n_sessions: number;
  revisions: Record<string, string[]>;
  mixed_revisions: boolean;
  revision_boundaries: Record<string, RevisionBoundary[]>;
  baro: { spec: string; min_kpa: number | null; max_kpa: number | null; sessions_without: string[] };
  gates: GateReport | null;
  [key: string]: unknown;
}

export interface NoiseFloor {
  available: boolean;
  channel: string;
  reason?: string;
  unit?: string;
  sd?: number;
  mean?: number;
  n_cells?: number;
  min?: number | null;
  max?: number | null;
  conditions?: string;
}

export type UnavailableKind =
  | "not_captured"
  | "refused"
  | "over_budget"
  | "gates_removed_everything"
  | "no_sessions"
  | null;

export interface Selection {
  sessions: string[];
  omitted_by_limit: string[];
  limit: number;
}

interface AnalysisBase {
  available: boolean;
  unavailable_kind: UnavailableKind;
  gates: GateReport | null;
  unavailable: UnavailableSession[];
  conditions: Conditions;
  selection?: Selection;
}

export interface DeliveryBand {
  rpm_band: string;
  n: number;
  commanded: number | null;
  measured: number | null;
  error_pct: number | null;
}

export interface DeliveryErrorResponse extends AnalysisBase {
  measured: string;
  min_load: number | null;
  n: number;
  band_averaged_pct?: number | null;
  pooled_pct?: number | null;
  bands: DeliveryBand[];
  per_session: Array<{ session_id: string; n: number; revision: string | null; gates: GateReport }>;
  noise_floor: NoiseFloor;
}

export interface Cell {
  rpm_index: number;
  airflow_index: number;
  rpm_range: [number, number];
  airflow_range: [number, number];
  n: number;
  sparse: boolean;
  [value: string]: number | boolean | [number, number] | null;
}

export interface Axes {
  rpm: string;
  airflow: string;
  rpm_edges: number[];
  airflow_edges: number[];
}

export interface TrimSurfaceResponse extends AnalysisBase {
  value: string;
  axes: Axes;
  min_n: number;
  n: number;
  banks: Record<string, { n: number; cells: Cell[] }>;
  per_session: Array<{ session_id: string; revision: string | null; cells: Record<string, Cell[]> }>;
}

export interface TimingResponse extends AnalysisBase {
  value: string;
  axes: Axes;
  min_n: number;
  n: number;
  cells: Cell[];
}

export interface ComparedCell {
  rpm_index: number;
  airflow_index: number;
  rpm_range: [number, number];
  airflow_range: [number, number];
  n_a: number;
  n_b: number;
  mean_a: number;
  mean_b: number;
  delta: number;
  dropped: boolean;
  delta_sigma: number | null;
}

export interface PullComparisonResponse extends AnalysisBase {
  value: string;
  revision_a: string;
  revision_b: string;
  axes: Axes;
  min_n: number;
  cells: ComparedCell[];
  n_cells: number;
  noise_floor: NoiseFloor | null;
}

/** Why a whole-session read produced nothing. Each kind has its own words on
 *  the page — "deferred while recording" is not "the gates removed
 *  everything", and neither is "this capture never polled the channel". */
export interface Refusal {
  kind: "deferred" | "busy" | "unavailable" | "refused" | "no_sessions" | "not_found" | "error";
  message: string;
  profile?: string;
  laptopPath?: string;
}

export function describeRefusal(error: unknown): Refusal {
  if (!(error instanceof HttpError)) {
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
  let detail: unknown = null;
  try {
    detail = (JSON.parse(error.body) as { detail?: unknown }).detail;
  } catch {
    detail = null;
  }
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    const message = typeof d.message === "string" ? d.message : error.message;
    switch (d.error) {
      case "analysis_deferred":
        return {
          kind: "deferred",
          message,
          profile: typeof d.profile === "string" ? d.profile : undefined,
          laptopPath: typeof d.laptop_path === "string" ? d.laptop_path : undefined,
        };
      case "analysis_busy":
        return { kind: "busy", message };
      case "analysis_unavailable":
        return { kind: "unavailable", message };
      case "no_sessions":
        return { kind: "no_sessions", message };
      case "session_not_found":
        return { kind: "not_found", message };
      default:
        return { kind: "refused", message };
    }
  }
  if (typeof detail === "string") {
    return { kind: error.status === 404 ? "not_found" : "refused", message: detail };
  }
  return { kind: "error", message: error.message };
}

/** "0 of N samples passed: <gate> removed M, …" — the gates-removed-everything
 *  sentence, from a gate report. */
export function gateSentence(gates: GateReport | null | undefined): string {
  if (!gates) return "no gate report";
  const removed = Object.entries(gates.filtered)
    .filter(([, count]) => count > 0)
    .map(([gate, count]) => `${gate.replace(/_/g, " ")} removed ${count}`);
  return `${gates.remaining} of ${gates.started} samples passed${removed.length ? `: ${removed.join(", ")}` : ""}`;
}

function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export function getLogs(token: string, includeSessions: boolean, signal?: AbortSignal) {
  return controlJson<LogsResponse>(
    `/control/logs${query({ include_sessions: includeSessions })}`,
    token,
    { signal },
  );
}

export function getFrame(
  token: string,
  sessionId: string,
  params: { specs: string[]; grid: string; regime: Regime; offset: number },
  signal?: AbortSignal,
) {
  return controlJson<FrameResponse>(
    `/control/sessions/${encodeURIComponent(sessionId)}/frame${query({
      specs: params.specs.join(","),
      grid: params.grid,
      regime: params.regime,
      offset: params.offset,
    })}`,
    token,
    { signal },
  );
}

export interface SessionChoice {
  sessions: string[];
  revision: string | null;
}

function choiceParams(choice: SessionChoice) {
  return { sessions: choice.sessions.join(","), revision: choice.revision };
}

export function getDeliveryError(
  token: string,
  choice: SessionChoice,
  measured: string,
  minLoad: number | null,
) {
  return controlJson<DeliveryErrorResponse>(
    `/analysis/delivery-error${query({ ...choiceParams(choice), measured, min_load: minLoad })}`,
    token,
  );
}

export function getTrimSurface(token: string, choice: SessionChoice) {
  return controlJson<TrimSurfaceResponse>(
    `/analysis/trim-surface${query(choiceParams(choice))}`,
    token,
  );
}

export function getTiming(token: string, choice: SessionChoice) {
  return controlJson<TimingResponse>(`/analysis/timing${query(choiceParams(choice))}`, token);
}

export function getPullComparison(
  token: string,
  revisionA: string,
  revisionB: string,
  floorChannel: string | null,
) {
  return controlJson<PullComparisonResponse>(
    `/analysis/pull-comparison${query({
      revision_a: revisionA,
      revision_b: revisionB,
      floor_channel: floorChannel,
    })}`,
    token,
  );
}

/** CSV of exactly the rows and columns given — the table's current view. */
export function toCsv(
  header: string[],
  rows: Array<Array<string | number | boolean | null>>,
): string {
  const escape = (value: string | number | boolean | null) => {
    if (value === null) return "";
    const text = String(value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  return [header, ...rows].map((row) => row.map(escape).join(",")).join("\n") + "\n";
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "unknown";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

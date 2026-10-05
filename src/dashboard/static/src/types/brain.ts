/** `/control/brain/*` — the second brain on the Pi (E04.5,
 *  `src/control/brain_reads.py`). HTTP and MCP serve these same bodies. */

export type BrainJobState = "queued" | "running" | "deferred" | "pending" | "completed" | "failed";

export interface BrainJob {
  job_id: string;
  kind: "twin_session_evaluation" | "memory_consolidation";
  params: Record<string, unknown>;
  state: BrainJobState;
  created_at: string;
  updated_at: string;
  attempts: number;
  reason: string | null;
  detail: string | null;
  progress: Record<string, unknown>;
  result: { evidence?: string[]; result_file?: string; computed_at?: string } | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface BrainJobsResponse {
  jobs: BrainJob[];
  counts: Partial<Record<BrainJobState, number>>;
}

export interface BrainStatus {
  vehicle: string;
  identity: {
    program_id: string | null;
    program_status: string;
    calibration: string | null;
    image_sha256: string | null;
  };
  change: string;
  last_change: { kind: string; at: string };
  program_specific_state: { valid: boolean; valid_since: string; detail: string };
  predictions: { calibration: string | null; valid_since: string; detail: string };
  tables: { source: string | null; reason: string | null; detail?: string };
  ram_addresses: { status: string; detail: string };
  transient: { state: string; reason: string; detail: string };
  memory: { records: number; current: number };
  jobs: Partial<Record<BrainJobState, number>>;
  /** The live twin as the inference tick last published it (E04.1; the same
   *  block as `/status` `detectors.inference.twin`). */
  live_twin?: LiveTwin;
}

export interface LiveTwinSample {
  acquired_at: string;
  observed_phi: number | null;
  predicted_phi: number | null;
  difference_phi: number | null;
  qualified_residual_phi: number | null;
  state: string;
  reason: string | null;
  branch: string | null;
}

export interface LiveTwin {
  enabled: boolean;
  result: string;
  reason?: string | null;
  detail?: string;
  session?: string;
  replay?: boolean;
  vehicle_state?: string | null;
  applicability?: { qualified: boolean; qualification_blockers: string[] };
  accounting?: { candidates: number; states: Record<string, number> };
  stream?: { pending: number; late_reads: Record<string, number> };
  intake?: { dropped: number; busy_ticks: number };
  latest_prediction?: LiveTwinSample | null;
}

export interface CardValue {
  name: string;
  value: number;
  unit: string;
  uncertainty: number | null;
  record_id: string;
}

export interface EvidenceView {
  id: string;
  tool: string;
  tool_version: string;
  subject: string;
  regime: string;
  status: "evaluated" | "unevaluable";
  reason: string | null;
  detail: string;
  values: CardValue[];
  residual: CardValue | null;
  labels: Record<string, string>;
  n_observations: number;
  standing: "current" | "superseded" | "retracted";
  sources: { session: string; window_start: string; window_end: string }[];
}

export interface InvestigationCard {
  investigation: string;
  title: string;
  question: string;
  status: string;
  regime: string;
  evidence_state: string;
  calibration: string | null;
  program_id: string | null;
  what_happened: EvidenceView[];
  hypotheses: {
    label: string;
    status: string;
    supporting: EvidenceView[];
    refuting: EvidenceView[];
    conflicting: boolean;
  }[];
  unknowns: string[];
  next_measurements: string[];
  narrative: string | null;
  narrative_notice: string | null;
}

export interface InvestigationCardResponse {
  card: InvestigationCard;
  parts: { ecu_computation: string[]; engine_response: string[] };
}

export interface MemoryRow {
  record: {
    id: string;
    kind: string;
    title?: string;
    subject?: string;
    recorded_at: string;
    status?: string;
  };
  superseded_at: string | null;
  retracted_by: string | null;
}

export interface MemoryResponse {
  vehicle: string;
  count: number;
  records: MemoryRow[];
}

export interface TwinTracePoint {
  t: string;
  observed_phi: number | null;
  predicted_phi: number | null;
  state: string;
  reason: string | null;
  branch: string | null;
  qualified_residual_phi: number | null;
}

export interface TwinEvaluation {
  slot: number;
  evidence_id: string;
  qualification_blockers: string[];
  accounting: {
    candidates: number;
    states: Record<string, number>;
    reasons: Record<string, Record<string, number>>;
  };
  capture_requirement: { reason: string; message: string } | null;
  missing_inputs: string[];
  trace: TwinTracePoint[];
  trace_omitted: number;
}

export interface TwinSessionResponse {
  session: string;
  job_id: string;
  state: BrainJobState | "not_evaluated";
  job: BrainJob | null;
  result: {
    calibration: string | null;
    program_id: string | null;
    evaluations: TwinEvaluation[];
    refusals: { slot: number; reason: string; detail: string; id: string }[];
  } | null;
  currency?: { program_current: boolean; program_detail: string };
}

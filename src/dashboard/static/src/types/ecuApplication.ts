/** `GET /control/ecu/application-model` — the ECM application twin's
 *  coverage manifest (E05.1, `src/analysis/ecu_application.py`
 *  `coverage_report`). MCP `get_ecu_application_model` returns the same body. */

/** How the twin stands to one traced node. */
export type TwinRole = "implemented" | "observed" | "assumed" | "not_modelled" | "calibration_dead";

export interface ApplicationNode {
  id: string;
  function: string;
  kind: "computation" | "state" | "copy";
  twin: TwinRole;
  confidence: "observed" | "code" | "inferred" | "unknown";
  implemented_by: string | null;
  observed_as: string[];
  assumption: string | null;
}

export interface ApplicationBlocker {
  name: string;
  nodes: string[];
  closes_with: string;
}

export interface ApplicationRegion {
  id: string;
  status: "unevaluable" | "conditional";
  when: string;
  nodes: string[];
}

export interface ApplicationOutput {
  id: string;
  ram: string;
  units: string;
  role: string;
  /** `qualified` only with no blockers and an observed channel
   *  (`ApplicationOutput.status`). */
  status: "qualified" | "conditional" | "unobserved";
  observed_as: { spec: string; relation: string; confidence: string; note?: string }[];
  twin: { definition: string; predicts: string } | null;
  path: ApplicationNode[];
  by_role: Partial<Record<TwinRole, string[]>>;
  blockers: ApplicationBlocker[];
  equals_output_when: { id: string; statement: string; settled_by: string }[];
  regions: ApplicationRegion[];
}

export interface UncoveredLogic {
  id: string;
  where: string;
  blocks: string;
  next_evidence: string;
}

export interface ApplicationModelReport {
  model: { name: string; schema_version: number; model_version: number; sha256: string };
  program: {
    id: string;
    calibration_id: string;
    identity_sha256: string;
    patch: string;
    images: { sha256: string; revision: string; role: string }[];
  };
  /** The image recorded as running now, when the flash history knows it. */
  image: { sha256: string; known: boolean; revision: string | null; role: string | null } | null;
  schedule: { function: string; cycle_start: string; order: string[] };
  outputs: ApplicationOutput[];
  uncovered: UncoveredLogic[];
}

// LLM-assist structured cards (Task S.1/S.2/S.5, docs/roadmap/llm-assist-and-
// compute-roadmap.md; E01.2). Mirrors src/analysis/llm_assist/cards.py —
// cards cite evidence ids minted by analysis tools, and the server resolves
// each card against the vehicle's memory ledger. Numbers are rendered from
// the resolved evidence records (`CardView.evidence`), never from card text.

export interface EvidenceCitation {
  evidence_id: string;
  /** Why the evidence matters, in words — the schema refuses numbers here. */
  note: string;
  /** Values the producer asserted; the server rejects any that differ. */
  quoted: Record<string, number>;
}

export interface DiagnosticHypothesis {
  rank: number;
  label: string;
  confidence: number | null;
  supporting_evidence: EvidenceCitation[];
  refuting_evidence: EvidenceCitation[];
}

export interface DifferentialCard {
  schema: "llm-assist-card/2";
  card_type: "differential";
  vehicle: string;
  generated_at: string;
  session: string | null;
  hypotheses: DiagnosticHypothesis[];
  /** The mandatory "hypotheses, not verdicts" disclaimer — always present,
   *  schema-locked on the Python side. Render it, don't paraphrase it. */
  framing: string;
  model_name: string | null;
}

export interface NarrativeSection {
  heading: string;
  body: string;
  evidence: EvidenceCitation[];
}

export interface NarrativeCard {
  schema: "llm-assist-card/2";
  card_type: "narrative";
  vehicle: string;
  generated_at: string;
  title: string;
  sections: NarrativeSection[];
  source_sessions: string[];
  model_name: string | null;
}

/** A card written before evidence ids: kept verbatim, labelled, never resolved. */
export interface UnverifiedHistoricalCard {
  card_type: "unverified_historical";
  original_card_type: string;
  generated_at: string | null;
  notice: string;
  original: Record<string, unknown>;
}

export type LlmAssistCard = DifferentialCard | NarrativeCard;

export interface EvidenceQuantity {
  value: number;
  unit: string;
  uncertainty: number | null;
}

export interface EvidenceResult {
  status: "evaluated" | "unevaluable";
  reason: string | null;
  detail: string;
  residual: EvidenceQuantity | null;
}

/** One resolved evidence record, as the server renders it. */
export interface ResolvedEvidence {
  id: string;
  tool: string;
  tool_version: string;
  subject: string;
  conditions: { regime: string; vehicle_state: string | null; operating_mode: string | null };
  valid_from: string;
  valid_to: string | null;
  recorded_at: string;
  sources: { session: string; window_start: string; window_end: string; channels: string[] }[];
  values: Record<string, EvidenceQuantity>;
  labels: Record<string, string>;
  quality: { n_observations: number; timestamp_basis: string | null; flags: string[] };
  result: EvidenceResult;
  superseded_at: string | null;
  superseded_by: string[];
  retracted_by: string | null;
}

export type CardView =
  | {
      /** `retracted`: every citation resolved, but a cited record has since
       *  been retracted — `problems` names which. */
      verification: "verified" | "retracted" | "rejected";
      problems: string[];
      card: LlmAssistCard;
      evidence: Record<string, ResolvedEvidence>;
    }
  | {
      verification: "unverified_historical";
      problems: string[];
      card: UnverifiedHistoricalCard;
      evidence: Record<string, ResolvedEvidence>;
    };

/** `GET /analysis/llm-assist/cards` — `enabled: false` is the explicit,
 *  expected absent state (the tier's default), not an error. */
export type LlmAssistCardsResponse = { enabled: false } | { enabled: true; cards: CardView[] };

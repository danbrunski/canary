// LLM-assist structured cards (Task S.1/S.2, docs/roadmap/llm-assist-and-
// compute-roadmap.md). Mirrors src/analysis/llm_assist/cards.py exactly —
// no producer exists yet, so `enabled: false` (the default) is the only
// state most deployments will ever see; the shape below is the seam a
// future producer fills in.

export interface EvidenceReference {
  description: string;
  channel: string | null;
  session: string | null;
  timestamp: string | null;
  detail: string | null;
}

export interface DiagnosticHypothesis {
  rank: number;
  label: string;
  confidence: number | null;
  supporting_evidence: EvidenceReference[];
  refuting_evidence: EvidenceReference[];
}

export interface DifferentialCard {
  card_type: "differential";
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
}

export interface NarrativeCard {
  card_type: "narrative";
  generated_at: string;
  title: string;
  sections: NarrativeSection[];
  source_sessions: string[];
  model_name: string | null;
}

export type LlmAssistCard = DifferentialCard | NarrativeCard;

/** `GET /analysis/llm-assist/cards` — `enabled: false` is the explicit,
 *  expected absent state (the tier's default), not an error. */
export type LlmAssistCardsResponse = { enabled: false } | { enabled: true; cards: LlmAssistCard[] };

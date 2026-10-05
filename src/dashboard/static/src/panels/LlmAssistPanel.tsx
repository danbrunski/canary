import { useCallback } from "react";

import { getLlmAssistCards } from "../api/http";
import { Panel } from "../components/Panel";
import { ResourceGate } from "../components/ResourceGate";
import { useControlResource } from "../hooks/useControlResource";
import { badgeClass, cn, placeholderClass } from "../styles";
import type {
  CardView,
  DifferentialCard,
  DiagnosticHypothesis,
  EvidenceCitation,
  LlmAssistCardsResponse,
  NarrativeCard,
  ResolvedEvidence,
  UnverifiedHistoricalCard,
} from "../types";

/** LLM-assist structured cards (Task S.2, docs/roadmap/llm-assist-and-
 *  compute-roadmap.md). Public/read-only, same shape as `MonitorsPanel` /
 *  `ExplorePanel` — no token to gate on. The tier itself is feature-flagged
 *  server-side (`config.llm_assist.enabled`, default off): no producer
 *  exists yet, so `enabled: false` is the expected response almost
 *  everywhere this ships, not an error — the absent-state message below is
 *  the explicit, designed-for default, not a fallback for a broken fetch.
 *
 *  Every number shown comes from a resolved evidence record (S.5, E01.2):
 *  card prose cannot carry one. A card whose citations did not resolve is
 *  shown as rejected with the server's reasons, and a card from before
 *  evidence ids as unverified historical narrative. */
export function LlmAssistPanel() {
  const fetcher = useCallback((signal: AbortSignal) => getLlmAssistCards(signal), []);
  const { data, error, loading } = useControlResource(fetcher, { intervalMs: 15000 });

  return (
    <Panel title="LLM Assist">
      <ResourceGate
        loading={loading}
        loadingMessage="checking LLM-assist tier…"
        error={error}
        data={data}
      >
        {(data: LlmAssistCardsResponse) =>
          data.enabled ? (
            <EnabledView cards={data.cards} />
          ) : (
            <p className={placeholderClass}>
              LLM-assist tier is disabled (the default — no producer is installed). Enable{" "}
              <code>llm_assist.enabled</code> in config once a producer exists to see
              differential/narrative cards here.
            </p>
          )
        }
      </ResourceGate>
    </Panel>
  );
}

type EvidenceMap = Record<string, ResolvedEvidence>;

function EnabledView({ cards }: { cards: CardView[] }) {
  if (cards.length === 0) {
    return <p className={placeholderClass}>No cards yet.</p>;
  }
  return (
    <div className="flex flex-col gap-3">
      {cards.map((view, index) => (
        <CardFrame key={index} view={view} />
      ))}
    </div>
  );
}

function CardFrame({ view }: { view: CardView }) {
  if (view.verification === "unverified_historical") {
    return <HistoricalCardView card={view.card} />;
  }
  if (view.verification === "rejected") {
    return (
      <div className="rounded border border-border-soft p-3">
        <div className="mb-2 flex items-center gap-2">
          <span className={badgeClass}>rejected</span>
          <span className="text-[0.78rem] text-fg-dim">{view.card.generated_at}</span>
        </div>
        <p className="text-[0.8rem] text-fg-dim">
          Not shown: this card cites evidence that does not resolve.
        </p>
        <ul className="ml-4 mt-1 list-disc text-[0.76rem] text-fg-faint">
          {view.problems.map((problem, index) => (
            <li key={index}>{problem}</li>
          ))}
        </ul>
      </div>
    );
  }
  const body =
    view.card.card_type === "differential" ? (
      <DifferentialCardView card={view.card} evidence={view.evidence} />
    ) : (
      <NarrativeCardView card={view.card} evidence={view.evidence} />
    );
  if (view.verification === "retracted") {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className={badgeClass}>retracted evidence</span>
          <span className="text-[0.78rem] text-fg-dim">
            This card rests on evidence that has since been retracted.
          </span>
        </div>
        <ul className="ml-4 list-disc text-[0.76rem] text-fg-faint">
          {view.problems.map((problem, index) => (
            <li key={index}>{problem}</li>
          ))}
        </ul>
        {body}
      </div>
    );
  }
  return body;
}

function HistoricalCardView({ card }: { card: UnverifiedHistoricalCard }) {
  return (
    <div className="rounded border border-border-soft p-3">
      <div className="mb-2 flex items-center gap-2">
        <span className={badgeClass}>unverified historical</span>
        <span className="text-[0.78rem] text-fg-dim">{card.generated_at ?? "undated"}</span>
      </div>
      <p className={cn(placeholderClass, "text-[0.72rem] italic")}>{card.notice}</p>
    </div>
  );
}

function DifferentialCardView({
  card,
  evidence,
}: {
  card: DifferentialCard;
  evidence: EvidenceMap;
}) {
  return (
    <div className="rounded border border-border-soft p-3">
      <div className="mb-2 flex items-center gap-2">
        <span className={badgeClass}>differential</span>
        <span className="text-[0.78rem] text-fg-dim">{card.generated_at}</span>
      </div>
      <ol className="flex flex-col gap-2">
        {card.hypotheses.map((hypothesis) => (
          <HypothesisRow key={hypothesis.rank} hypothesis={hypothesis} evidence={evidence} />
        ))}
      </ol>
      <p className={cn(placeholderClass, "mt-2 text-[0.72rem] italic")}>{card.framing}</p>
    </div>
  );
}

function HypothesisRow({
  hypothesis,
  evidence,
}: {
  hypothesis: DiagnosticHypothesis;
  evidence: EvidenceMap;
}) {
  return (
    <li className="text-[0.85rem]">
      <span className="font-num text-fg-dim">#{hypothesis.rank}</span>{" "}
      <span className="text-fg">{hypothesis.label}</span>
      {hypothesis.confidence != null && (
        <span className="ml-2 text-fg-faint">({Math.round(hypothesis.confidence * 100)}%)</span>
      )}
      <CitationList
        label="Supporting"
        citations={hypothesis.supporting_evidence}
        evidence={evidence}
      />
      {hypothesis.refuting_evidence.length > 0 && (
        <CitationList
          label="Refuting"
          citations={hypothesis.refuting_evidence}
          evidence={evidence}
        />
      )}
    </li>
  );
}

function formatValue(name: string, quantity: { value: number; unit: string }): string {
  const unit = quantity.unit === "1" ? "" : ` ${quantity.unit}`;
  return `${name} ${quantity.value.toPrecision(4)}${unit}`;
}

function CitationList({
  label,
  citations,
  evidence,
}: {
  label: string;
  citations: EvidenceCitation[];
  evidence: EvidenceMap;
}) {
  return (
    <ul className="ml-4 mt-1 flex flex-col gap-0.5 text-[0.76rem] text-fg-dim">
      {citations.map((citation, index) => (
        <li key={index}>
          {label}: {citation.note || citation.evidence_id}
          <EvidenceSummary record={evidence[citation.evidence_id]} />
        </li>
      ))}
    </ul>
  );
}

function EvidenceSummary({ record }: { record: ResolvedEvidence | undefined }) {
  if (record === undefined) {
    return null;
  }
  const result = record.result;
  const body =
    result.status === "unevaluable"
      ? `unevaluable (${result.reason ?? "unknown"}): ${result.detail}`
      : Object.entries(record.values)
          .map(([name, quantity]) => formatValue(name, quantity))
          .join(", ");
  const superseded =
    record.retracted_by !== null
      ? " — retracted"
      : record.superseded_at !== null
        ? " — superseded"
        : "";
  return (
    <span className="ml-1 text-fg-faint">
      [{record.tool}, {record.conditions.regime}] {body}
      {superseded}
    </span>
  );
}

function NarrativeCardView({ card, evidence }: { card: NarrativeCard; evidence: EvidenceMap }) {
  return (
    <div className="rounded border border-border-soft p-3">
      <div className="mb-2 flex items-center gap-2">
        <span className={badgeClass}>narrative</span>
        <span className="text-[0.9rem] font-bold text-fg">{card.title}</span>
      </div>
      {card.sections.map((section) => (
        <div key={section.heading} className="mb-2">
          <h3 className="mb-1 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
            {section.heading}
          </h3>
          <p className="text-[0.85rem] text-fg">{section.body}</p>
          {section.evidence.length > 0 && (
            <CitationList label="Evidence" citations={section.evidence} evidence={evidence} />
          )}
        </div>
      ))}
      <p className={cn(placeholderClass, "text-[0.72rem]")}>
        Sources: {card.source_sessions.join(", ")}
      </p>
    </div>
  );
}

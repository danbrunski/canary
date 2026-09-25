import { useCallback } from "react";

import { getLlmAssistCards } from "../api/http";
import { Panel } from "../components/Panel";
import { ResourceGate } from "../components/ResourceGate";
import { useControlResource } from "../hooks/useControlResource";
import { badgeClass, cn, placeholderClass } from "../styles";
import type {
  DifferentialCard,
  DiagnosticHypothesis,
  EvidenceReference,
  LlmAssistCard,
  LlmAssistCardsResponse,
  NarrativeCard,
} from "../types";

/** LLM-assist structured cards (Task S.2, docs/roadmap/llm-assist-and-
 *  compute-roadmap.md). Public/read-only, same shape as `MonitorsPanel` /
 *  `ExplorePanel` — no token to gate on. The tier itself is feature-flagged
 *  server-side (`config.llm_assist.enabled`, default off): no producer
 *  exists yet, so `enabled: false` is the expected response almost
 *  everywhere this ships, not an error — the absent-state message below is
 *  the explicit, designed-for default, not a fallback for a broken fetch. */
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

function EnabledView({ cards }: { cards: LlmAssistCard[] }) {
  if (cards.length === 0) {
    return <p className={placeholderClass}>No cards yet.</p>;
  }
  return (
    <div className="flex flex-col gap-3">
      {cards.map((card, index) =>
        card.card_type === "differential" ? (
          <DifferentialCardView key={index} card={card} />
        ) : (
          <NarrativeCardView key={index} card={card} />
        ),
      )}
    </div>
  );
}

function DifferentialCardView({ card }: { card: DifferentialCard }) {
  return (
    <div className="rounded border border-border-soft p-3">
      <div className="mb-2 flex items-center gap-2">
        <span className={badgeClass}>differential</span>
        <span className="text-[0.78rem] text-fg-dim">{card.generated_at}</span>
      </div>
      <ol className="flex flex-col gap-2">
        {card.hypotheses.map((hypothesis) => (
          <HypothesisRow key={hypothesis.rank} hypothesis={hypothesis} />
        ))}
      </ol>
      <p className={cn(placeholderClass, "mt-2 text-[0.72rem] italic")}>{card.framing}</p>
    </div>
  );
}

function HypothesisRow({ hypothesis }: { hypothesis: DiagnosticHypothesis }) {
  return (
    <li className="text-[0.85rem]">
      <span className="font-num text-fg-dim">#{hypothesis.rank}</span>{" "}
      <span className="text-fg">{hypothesis.label}</span>
      {hypothesis.confidence != null && (
        <span className="ml-2 text-fg-faint">({Math.round(hypothesis.confidence * 100)}%)</span>
      )}
      <EvidenceList label="Supporting" evidence={hypothesis.supporting_evidence} />
      {hypothesis.refuting_evidence.length > 0 && (
        <EvidenceList label="Refuting" evidence={hypothesis.refuting_evidence} />
      )}
    </li>
  );
}

function EvidenceList({ label, evidence }: { label: string; evidence: EvidenceReference[] }) {
  return (
    <ul className="ml-4 mt-1 flex flex-col gap-0.5 text-[0.76rem] text-fg-dim">
      {evidence.map((item, index) => (
        <li key={index}>
          {label}: {item.description}
        </li>
      ))}
    </ul>
  );
}

function NarrativeCardView({ card }: { card: NarrativeCard }) {
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
        </div>
      ))}
      <p className={cn(placeholderClass, "text-[0.72rem]")}>
        Sources: {card.source_sessions.join(", ")}
      </p>
    </div>
  );
}

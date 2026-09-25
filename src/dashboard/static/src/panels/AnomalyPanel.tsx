import { useCallback, useMemo, useState } from "react";

import { getFeedback, postFeedback } from "../api/http";
import { Panel } from "../components/Panel";
import { Sparkline } from "../components/Sparkline";
import { useControlResource } from "../hooks/useControlResource";
import { scoreView, windowReference, type AnomalyLevel } from "../anomalyScore";
import type { ScoresBySource } from "../hooks/useScoreStream";
import { badgeClass, cn, inputClass, placeholderClass, selectClass } from "../styles";
import type { AnomalyEvent } from "../types";
import { formatTimestamp } from "../util/time";
import { LiveModelScores } from "./LiveModelScores";
import { channelRankingCaption, noveltyScreen, relatedEventsFor } from "../lib/evidence";
import { ModelEvidencePanel } from "./ModelEvidencePanel";

const LEVEL_TONE: Record<AnomalyLevel, string> = {
  low: "text-fg-dim",
  elevated: "text-warn",
  high: "text-bad",
};

interface AnomalyPanelProps {
  events: AnomalyEvent[];
  /** When a control token is present the timeline becomes labellable
   *  (Task 6.24): each event gets true/false-positive / ignore / review
   *  verdicts that persist next to the anomaly log. */
  token?: string;
  /** Live per-tick scores for both models (from `/ws/scores`). Drives the
   *  always-on readout at the top, independent of the emitted-anomaly list. */
  liveScores?: ScoresBySource;
}

const FEEDBACK_LABELS: { id: string; short: string; tone: string }[] = [
  { id: "true_positive", short: "TP", tone: "text-bad" },
  { id: "false_positive", short: "FP", tone: "text-ok" },
  { id: "ignored", short: "ignore", tone: "text-fg-dim" },
  { id: "needs_review", short: "review", tone: "text-warn" },
];

function eventKey(e: AnomalyEvent): string {
  return `${e.timestamp}|${e.source}|${e.model_version}`;
}

/** Anomaly timeline (Task 5.10): the most recent score chart up top, then a
 *  filterable list of events. The chart is fixed-width historical — newest
 *  events on the right — so an operator at a glance can see "are we
 *  trending hotter?" without parsing timestamps. */
export function AnomalyPanel({ events, token, liveScores }: AnomalyPanelProps) {
  const [sourceFilter, setSourceFilter] = useState<AnomalyEvent["source"] | "all">("all");
  const [modeFilter, setModeFilter] = useState<string | "all">("all");
  // The live readout up top now answers "what's it doing right now", so the
  // historical list shows everything by default (operators didn't want events
  // hidden). The toggle still lets them collapse the near-zero IF boundary
  // noise when scanning history.
  const [showAll, setShowAll] = useState(true);
  // Locally-applied verdicts (optimistic) keyed by anomaly key, merged over
  // whatever the server already has.
  const [localLabels, setLocalLabels] = useState<Record<string, string>>({});
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  // Task S.3 (llm-assist-and-compute-roadmap.md): the operator's working
  // theory + an optional clarifying question/answer pair, attached to
  // whichever event the label buttons are next clicked for. Cleared after
  // a successful submit.
  const [hypothesis, setHypothesis] = useState("");
  const [clarifyingQuestion, setClarifyingQuestion] = useState("");
  const [clarifyingAnswer, setClarifyingAnswer] = useState("");
  // Backend pairing rule (AnomalyFeedback._clarifying_pair_is_complete):
  // both set or both empty — never just one half of the pair.
  const hasQuestion = clarifyingQuestion.trim() !== "";
  const hasAnswer = clarifyingAnswer.trim() !== "";
  const clarifyingPairIncomplete = hasQuestion !== hasAnswer;

  const feedbackFetcher = useCallback(
    (signal: AbortSignal) => getFeedback(token ?? "", signal),
    [token],
  );
  const feedback = useControlResource(feedbackFetcher, {
    enabled: Boolean(token),
    intervalMs: 30000,
  });

  const serverLabels = useMemo(() => {
    const m: Record<string, string> = {};
    for (const fb of feedback.data?.feedback ?? []) m[fb.anomaly_key] = fb.label;
    return m;
  }, [feedback.data]);

  const labelEvent = async (e: AnomalyEvent, label: string) => {
    if (!token || clarifyingPairIncomplete) return;
    const key = eventKey(e);
    setLocalLabels((prev) => ({ ...prev, [key]: label }));
    try {
      await postFeedback(
        token,
        e as unknown as Record<string, unknown>,
        label,
        null,
        hypothesis.trim() || null,
        hasQuestion ? clarifyingQuestion.trim() : null,
        hasAnswer ? clarifyingAnswer.trim() : null,
      );
      feedback.refresh();
      // Enrichment is per-verdict, not per-draft — clear it once it's landed.
      setHypothesis("");
      setClarifyingQuestion("");
      setClarifyingAnswer("");
    } catch {
      // Roll back the optimistic mark on failure.
      setLocalLabels((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
    }
  };

  const visible = useMemo(() => {
    return events.filter((e) => {
      if (sourceFilter !== "all" && e.source !== sourceFilter) return false;
      if (modeFilter !== "all" && e.operating_mode !== modeFilter) return false;
      return true;
    });
  }, [events, sourceFilter, modeFilter]);

  // Normalize once per visible set: IF carries no threshold, so the index
  // self-calibrates against the window's own p95 (see anomalyScore.ts).
  const scored = useMemo(() => {
    const ref = windowReference(visible.map((e) => e.score));
    return visible.map((e) => ({ e, view: scoreView(e.score, e.threshold, ref) }));
  }, [visible]);

  // Chart keeps every point (continuity); the list is what gets de-flooded.
  const indexSeries = useMemo(
    () =>
      [...scored]
        .reverse()
        .slice(-60)
        .map((s) => s.view.index),
    [scored],
  );

  const shown = useMemo(
    () => (showAll ? scored : scored.filter((s) => s.view.level !== "low")),
    [scored, showAll],
  );
  const hiddenCount = scored.length - shown.length;

  const selectedEvent = useMemo(() => {
    if (shown.length === 0) return null;
    return shown.find(({ e }) => eventKey(e) === selectedKey)?.e ?? shown[0].e;
  }, [selectedKey, shown]);

  const relatedEvents = useMemo(
    () => relatedEventsFor(selectedEvent, visible),
    [selectedEvent, visible],
  );

  const modes = useMemo(() => {
    const set = new Set<string>();
    for (const e of events) if (e.operating_mode) set.add(e.operating_mode);
    return [...set].sort();
  }, [events]);

  return (
    <Panel
      title="Anomalies"
      centerHeader
      actions={
        <div className="flex flex-wrap items-center gap-3 font-num text-[0.78rem] tabular-nums text-fg-dim">
          {token && feedback.data && (
            <span title="operator verdicts on file">
              fb: {feedback.data.summary.false_positive ?? 0} FP /{" "}
              {feedback.data.summary.true_positive ?? 0} TP
            </span>
          )}
          <label className="flex items-center gap-1.5">
            source{" "}
            <select
              className={cn(selectClass, "min-h-0 rounded py-0.5 pl-1.5 pr-6")}
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value as typeof sourceFilter)}
            >
              <option value="all">all</option>
              <option value="fused">fused</option>
              <option value="behavior">behavior</option>
              <option value="isolation_forest">isolation_forest</option>
              <option value="lstm_autoencoder">lstm_autoencoder</option>
            </select>
          </label>
          {modes.length > 0 && (
            <label className="flex items-center gap-1.5">
              mode{" "}
              <select
                className={cn(selectClass, "min-h-0 rounded py-0.5 pl-1.5 pr-6")}
                value={modeFilter}
                onChange={(e) => setModeFilter(e.target.value)}
              >
                <option value="all">all</option>
                {modes.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
      }
    >
      <>
        <LiveModelScores scores={liveScores ?? {}} />
        <p className="mb-2 mt-0 text-[0.72rem] text-fg-faint">
          By default the Isolation Forest and the sequence model (LSTM) are evidence, not alarms:
          their events are stored and tagged{" "}
          <span className="uppercase tracking-[0.08em]">evidence</span>, never pushed. A sequence
          event says whether its window was outside the envelope the model trained on. The primary
          detector is the behavior models, with the plausibility checks for gross faults (operator
          decision 2026-09-23, models.roles).
        </p>
        {events.length === 0 ? (
          <p className={placeholderClass}>
            no anomalies emitted yet — the live scores above show current model state (the list
            below only logs threshold crossings).
          </p>
        ) : scored.length === 0 ? (
          <p className={placeholderClass}>no events match the current filters.</p>
        ) : (
          <>
            <div className="mb-2 flex gap-2">
              <div className="flex flex-col justify-between py-0.5 text-[0.6rem] tabular-nums text-fg-faint">
                <span>100</span>
                <span>50</span>
                <span>0</span>
              </div>
              <div className="flex flex-col gap-0.5">
                <Sparkline
                  values={indexSeries}
                  width={500}
                  height={72}
                  stroke="#FFB84A"
                  min={0}
                  max={100}
                  baseline={50}
                />
                <div className="flex justify-between text-[0.65rem] text-fg-faint">
                  <span>← older</span>
                  <span>50 ≈ model threshold · last {indexSeries.length}</span>
                  <span>newest →</span>
                </div>
              </div>
            </div>
            {hiddenCount > 0 && (
              <button
                type="button"
                className="mb-1 text-[0.72rem] text-accent hover:underline"
                onClick={() => setShowAll((v) => !v)}
              >
                {showAll
                  ? "show fewer (hide low-significance)"
                  : `${hiddenCount} low-significance hidden · show all`}
              </button>
            )}
            <ModelEvidencePanel event={selectedEvent} relatedEvents={relatedEvents} token={token} />
            {token && (
              <div className="mb-2 flex flex-wrap items-center gap-1.5">
                <input
                  className={cn(inputClass, "min-h-0 flex-1 basis-40 py-0.5 text-[0.78rem]")}
                  value={hypothesis}
                  onChange={(e) => setHypothesis(e.target.value)}
                  placeholder="hypothesis (optional) — attaches to the next verdict"
                  aria-label="hypothesis"
                />
                <input
                  className={cn(inputClass, "min-h-0 flex-1 basis-32 py-0.5 text-[0.78rem]")}
                  value={clarifyingQuestion}
                  onChange={(e) => setClarifyingQuestion(e.target.value)}
                  placeholder="clarifying question (optional)"
                  aria-label="clarifying question"
                />
                <input
                  className={cn(inputClass, "min-h-0 flex-1 basis-32 py-0.5 text-[0.78rem]")}
                  value={clarifyingAnswer}
                  onChange={(e) => setClarifyingAnswer(e.target.value)}
                  placeholder="clarifying answer (optional)"
                  aria-label="clarifying answer"
                />
                {clarifyingPairIncomplete && (
                  <span className="basis-full text-[0.72rem] text-bad">
                    clarifying question and answer must both be filled (or both left empty) before
                    marking a verdict.
                  </span>
                )}
              </div>
            )}
            <ol className="m-0 max-h-[22rem] list-none overflow-y-auto p-0">
              {shown.slice(0, 50).map(({ e, view }) => (
                <li
                  key={`${e.timestamp}-${e.source}-${e.model_version}`}
                  className={cn(
                    "flex flex-wrap items-baseline gap-2 border-b border-border py-1.5 text-[0.85rem] last:border-b-0",
                    selectedEvent !== null &&
                      eventKey(e) === eventKey(selectedEvent) &&
                      "bg-accent/5",
                  )}
                >
                  <time className="font-num tabular-nums text-fg-dim">
                    {formatTimestamp(e.timestamp)}
                  </time>
                  <span
                    className={cn(
                      "rounded bg-elev-2 px-1.5 py-0.5 text-[0.7rem] uppercase tracking-[0.08em]",
                      e.source === "isolation_forest" && "text-accent",
                      e.source === "lstm_autoencoder" && "text-ok",
                      e.source === "fused" && "text-warn",
                      e.source === "behavior" && "text-accent",
                    )}
                  >
                    {e.source.replace("_", " ")}
                  </span>
                  {e.role === "evidence" && (
                    <span
                      className="rounded border border-border px-1.5 py-0.5 text-[0.7rem] uppercase tracking-[0.08em] text-fg-dim"
                      title="From a detector configured as non-alerting (models.roles, A06.5): stored as low-weight evidence, never pushed or shown as an active alert."
                    >
                      evidence
                    </span>
                  )}
                  {e.operating_mode && (
                    <span className="text-[0.75rem] text-fg-dim">{e.operating_mode}</span>
                  )}
                  {noveltyScreen(e) && (
                    <span
                      className={cn(
                        "rounded border border-border px-1.5 py-0.5 text-[0.7rem]",
                        noveltyScreen(e)?.outside ? "text-warn" : "text-fg-dim",
                      )}
                      title={noveltyScreen(e)?.sentence}
                    >
                      {noveltyScreen(e)?.label}
                    </span>
                  )}
                  <span
                    className={cn("font-num tabular-nums", LEVEL_TONE[view.level])}
                    title={`raw score ${e.score.toFixed(3)}${
                      e.threshold !== null ? ` / threshold ${e.threshold.toFixed(3)}` : ""
                    }`}
                  >
                    idx {view.index}
                  </span>
                  <span className="font-num text-[0.72rem] tabular-nums text-fg-faint">
                    raw {e.score.toFixed(3)}
                    {e.threshold !== null && ` / ${e.threshold.toFixed(3)}`}
                  </span>
                  {e.affected_channels.length > 0 && (
                    <span
                      className="text-[0.78rem] text-fg-dim"
                      title={
                        channelRankingCaption(e)?.title ??
                        `Channels whose reconstruction error exceeded its calibrated band (${e.affected_channels.length}). Click the row for the full per-channel breakdown.`
                      }
                    >
                      {channelRankingCaption(e)?.caption ??
                        `${e.affected_channels.length} chan over band`}{" "}
                      · {e.affected_channels.slice(0, 2).join(", ")}
                      {e.affected_channels.length > 2 && ` +${e.affected_channels.length - 2}`}
                    </span>
                  )}
                  {e.detail?.missing_features === true && (
                    <span className={cn(badgeClass, "border-warn bg-warn text-bg")}>
                      missing features
                    </span>
                  )}
                  <button
                    type="button"
                    className="ml-auto rounded border border-border-soft px-1.5 py-0.5 text-[0.66rem] uppercase tracking-[0.04em] text-accent hover:border-border"
                    onClick={() => setSelectedKey(eventKey(e))}
                  >
                    inspect
                  </button>
                  {token &&
                    (() => {
                      const current = localLabels[eventKey(e)] ?? serverLabels[eventKey(e)];
                      return (
                        <span className="flex items-center gap-1">
                          {current ? (
                            <span className="text-[0.7rem] uppercase tracking-[0.06em] text-accent-2">
                              {current.replace("_", " ")}
                            </span>
                          ) : (
                            FEEDBACK_LABELS.map((fb) => (
                              <button
                                key={fb.id}
                                type="button"
                                disabled={clarifyingPairIncomplete}
                                className={cn(
                                  "rounded border border-border-soft px-1.5 py-0.5 text-[0.66rem] uppercase tracking-[0.04em] hover:border-border disabled:cursor-default disabled:opacity-50",
                                  fb.tone,
                                )}
                                title={`mark ${fb.id.replace("_", " ")}`}
                                aria-label={`mark ${fb.id.replace("_", " ")}`}
                                onClick={() => labelEvent(e, fb.id)}
                              >
                                {fb.short}
                              </button>
                            ))
                          )}
                        </span>
                      );
                    })()}
                </li>
              ))}
            </ol>
          </>
        )}
      </>
    </Panel>
  );
}

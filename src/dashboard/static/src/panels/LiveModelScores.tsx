import { scoreView, windowReference, type AnomalyLevel } from "../anomalyScore";
import { Sparkline } from "../components/Sparkline";
import type { ScoreSource, ScoresBySource } from "../hooks/useScoreStream";
import { cn } from "../styles";
import type { ScoreMessage } from "../types";

const LEVEL_TONE: Record<AnomalyLevel, string> = {
  low: "text-fg-dim",
  elevated: "text-warn",
  high: "text-bad",
};

const SOURCE_LABEL: Record<ScoreSource, string> = {
  isolation_forest: "isolation forest",
  lstm_autoencoder: "lstm autoencoder",
  fused: "fused severity",
  behavior: "behavior residual",
};

const SOURCES: ScoreSource[] = [
  "isolation_forest",
  "lstm_autoencoder",
  "fused",
  "behavior",
];

/** One model's live readout: current normalized index + level + raw value,
 *  and a fixed-scale (0–100, threshold ref at 50) sparkline of recent ticks.
 *  Updates every inference tick — a normal 0 shows as 0; a missing-feature
 *  tick shows "—/degraded" rather than a misleading number. */
function ModelCard({ source, msgs }: { source: ScoreSource; msgs: ScoreMessage[] | undefined }) {
  const buffer = msgs ?? [];
  const latest = buffer.length > 0 ? buffer[buffer.length - 1] : null;
  const ref = windowReference(buffer.map((m) => (m.score == null ? 0 : m.score)));
  const series = buffer.map((m) =>
    m.score == null ? 0 : scoreView(m.score, m.threshold, ref).index,
  );
  const view =
    latest && latest.score != null && !latest.missing_features
      ? scoreView(latest.score, latest.threshold, ref)
      : null;

  return (
    <div className="flex-1 rounded border border-border bg-elev-2 p-2">
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-[0.7rem] uppercase tracking-[0.08em] text-fg-dim">
          {SOURCE_LABEL[source]}
        </span>
        {latest?.operating_mode && (
          <span className="text-[0.7rem] text-fg-dim">{latest.operating_mode}</span>
        )}
      </div>
      {!latest ? (
        <div className="py-2 text-[0.8rem] text-fg-faint">waiting for ticks…</div>
      ) : !view ? (
        <div className="flex items-baseline gap-2">
          <span className="font-num text-2xl font-semibold tabular-nums text-fg-dim">—</span>
          <span className="text-[0.7rem] uppercase tracking-[0.06em] text-warn">
            degraded · missing features
          </span>
        </div>
      ) : (
        <div className="flex items-baseline gap-2">
          <span
            className={cn(
              "font-num text-2xl font-semibold tabular-nums",
              LEVEL_TONE[view.level],
            )}
          >
            {view.index}
            <span className="text-sm text-fg-dim">/100</span>
          </span>
          <span
            className={cn("text-[0.7rem] uppercase tracking-[0.06em]", LEVEL_TONE[view.level])}
          >
            {view.level}
          </span>
          <span className="font-num text-[0.72rem] tabular-nums text-fg-faint">
            raw {latest.score?.toFixed(3) ?? "—"}
            {latest.threshold != null && ` / thr ${latest.threshold.toFixed(3)}`}
          </span>
        </div>
      )}
      <div className="mt-1 flex gap-2">
        <div className="flex flex-col justify-between py-0.5 text-[0.55rem] tabular-nums text-fg-faint">
          <span>100</span>
          <span>0</span>
        </div>
        <Sparkline
          values={series}
          width={240}
          height={44}
          stroke="#FFB84A"
          min={0}
          max={100}
          baseline={50}
        />
      </div>
    </div>
  );
}

/** Live per-model score readout (both detectors), fed by `/ws/scores`. */
export function LiveModelScores({ scores }: { scores: ScoresBySource }) {
  return (
    <div className="mb-3 flex flex-col gap-2 sm:flex-row">
      {SOURCES.map((s) => (
        <ModelCard key={s} source={s} msgs={scores[s]} />
      ))}
    </div>
  );
}

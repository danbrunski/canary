import { useCallback } from "react";

import { getTrainingRuns } from "../api/http";
import { Panel } from "../components/Panel";
import { Sparkline } from "../components/Sparkline";
import { useControlResource } from "../hooks/useControlResource";
import { cn, placeholderClass } from "../styles";
import type { TrainingRun } from "../types";

interface TrainingPanelProps {
  token: string;
}

const PUBLISH_STYLE: Record<string, string> = {
  active: "text-ok",
  published: "text-accent-2",
  unpublished: "text-fg-faint",
  rolled_back: "text-bad",
};

/** Training dashboard (Task 6.22): run history + model quality from the
 *  training-run ledger (Task 6.21). Loss curve, threshold band, held-out
 *  FP rate and publish status per run. */
export function TrainingPanel({ token }: TrainingPanelProps) {
  const fetcher = useCallback(
    (signal: AbortSignal) => getTrainingRuns(token, signal),
    [token],
  );
  const { data, error, loading } = useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: 15000,
  });

  if (!token) {
    return (
      <Panel title="Training">
        <p className={placeholderClass}>
          Enter the control token (Control panel) to read training history.
        </p>
      </Panel>
    );
  }
  if (loading) {
    return (
      <Panel title="Training">
        <p className={placeholderClass}>loading runs…</p>
      </Panel>
    );
  }
  if (error || !data) {
    return (
      <Panel title="Training">
        <p className={cn(placeholderClass, "text-bad")}>{error ?? "no data"}</p>
      </Panel>
    );
  }
  if (data.runs.length === 0) {
    return (
      <Panel title="Training" count="0 runs">
        <p className={placeholderClass}>
          No training runs recorded yet. Run <code>scripts/retrain.py</code> or
          a <code>retrain_*</code> control job — each lands here automatically.
        </p>
      </Panel>
    );
  }

  // Newest first.
  const runs = [...data.runs].reverse();

  return (
    <Panel title="Training" count={`${data.count} runs`}>
      <div className="flex max-h-[34rem] flex-col gap-2 overflow-y-auto">
        {runs.map((run) => (
          <RunCard key={run.run_id} run={run} />
        ))}
      </div>
    </Panel>
  );
}

function RunCard({ run }: { run: TrainingRun }) {
  const loss = numberArray(run.metrics.loss_curve);
  const fp = run.metrics.false_positive_benchmark as
    | Record<string, unknown>
    | undefined;
  const fpRate =
    fp && typeof fp.false_positive_rate === "number"
      ? fp.false_positive_rate
      : null;
  const alertsPerHour =
    fp && typeof fp.alerts_per_hour === "number" ? fp.alerts_per_hour : null;

  return (
    <div className="rounded-[8px] border border-border-soft bg-[linear-gradient(180deg,var(--color-elev-2),var(--color-elev))] p-2.5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <b className="font-num text-[0.85rem]">{run.run_id}</b>
        <span
          className={cn(
            "text-[0.7rem] uppercase tracking-[0.08em]",
            PUBLISH_STYLE[run.publish_status] ?? "text-fg-dim",
          )}
        >
          {run.publish_status}
        </span>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[0.76rem] text-fg-dim">
        <span>{run.model_type}</span>
        <span className={run.status === "failed" ? "text-bad" : "text-ok"}>
          {run.status}
        </span>
        <span>{run.input_sessions.length} sessions</span>
        <span>{run.feature_schema.length} features</span>
        {fpRate !== null && (
          <span title="held-out false-positive rate">
            FP {(fpRate * 100).toFixed(1)}%
          </span>
        )}
        {alertsPerHour !== null && (
          <span>{alertsPerHour.toFixed(2)} alerts/hr</span>
        )}
        <time className="font-num">{run.created_at.slice(0, 19)}</time>
      </div>
      {loss.length > 1 && (
        <div className="mt-2">
          <span className="text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
            loss curve ({loss.length} epochs)
          </span>
          <Sparkline values={loss} width={420} height={56} stroke="#4FD6E0" />
        </div>
      )}
      {threshold(run) !== null && (
        <div className="mt-1 text-[0.74rem] text-fg-dim">
          calibrated threshold:{" "}
          <b className="font-num text-fg">{threshold(run)?.toFixed(4)}</b>
        </div>
      )}
    </div>
  );
}

function numberArray(value: unknown): number[] {
  return Array.isArray(value)
    ? value.filter((v): v is number => typeof v === "number")
    : [];
}

/** LSTM calibrated global threshold from the manifest metrics, if present. */
function threshold(run: TrainingRun): number | null {
  const t = run.metrics.thresholds;
  if (t && typeof t === "object") {
    const g = (t as Record<string, unknown>).global;
    if (g && typeof g === "object") {
      const v = (g as Record<string, unknown>).threshold;
      if (typeof v === "number") return v;
    }
  }
  return null;
}

import { useEffect, useRef, useState } from "react";

import { getJob, getJobResult, submitJob } from "../api/http";
import { describeError } from "../hooks/useControlResource";
import { badgeClass, buttonClass, cn, placeholderClass } from "../styles";
import type { AnomalyEvent, ReplayWindowReport } from "../types";

const POLL_MS = 2000;

type RescoreStatus = "idle" | "submitting" | "running" | "succeeded" | "failed";

interface RescoreState {
  status: RescoreStatus;
  jobId: string | null;
  error: string | null;
  report: ReplayWindowReport | null;
}

const IDLE: RescoreState = { status: "idle", jobId: null, error: null, report: null };

interface RescoreControlProps {
  token?: string;
  event: AnomalyEvent;
}

/** "Re-score this window" (Task 4.1): submits the same `replay_window` job
 *  the MCP tool wraps (`src/mcp_server/tools/replay.py:_build_params`),
 *  mirroring its param mapping — `session_id` + the event's own window
 *  bounds — polls it through the existing jobs plumbing, and renders the
 *  per-detector fired/score-vs-threshold report inline. Closes the "why
 *  did/didn't this fire?" loop without leaving the dashboard for MCP/CLI. */
export function RescoreControl({ token, event }: RescoreControlProps) {
  const [state, setState] = useState<RescoreState>(IDLE);
  // Reset whenever the operator inspects a different event — a stale
  // report from the last window must never look like it answers this one.
  const lastEventKey = useRef<string>("");
  const eventKey = `${event.timestamp}|${event.source}|${event.model_version}`;
  if (eventKey !== lastEventKey.current) {
    lastEventKey.current = eventKey;
    if (state.status !== "idle") setState(IDLE);
  }

  useEffect(() => {
    if (state.status !== "running" || !state.jobId || !token) return;
    const jobId = state.jobId;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const res = await getJob(token, jobId);
          if (cancelled) return;
          if (res.job.status === "succeeded") {
            const withResult = await getJobResult(token, jobId);
            if (cancelled) return;
            setState({ status: "succeeded", jobId, error: null, report: withResult.result });
          } else if (res.job.status === "failed" || res.job.status === "cancelled") {
            setState({
              status: "failed",
              jobId,
              error: `replay_window job ${res.job.status}`,
              report: null,
            });
          }
        } catch (err) {
          if (!cancelled) {
            setState({ status: "failed", jobId, error: describeError(err), report: null });
          }
        }
      })();
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [state.status, state.jobId, token]);

  if (!token) return null;

  const rescore = async () => {
    if (!event.session) return;
    setState({ status: "submitting", jobId: null, error: null, report: null });
    const params: Record<string, unknown> = { session_id: event.session };
    if (event.window_start_timestamp) params.start = event.window_start_timestamp;
    if (event.window_end_timestamp) params.end = event.window_end_timestamp;
    try {
      const submitted = await submitJob(token, "replay_window", params);
      setState({ status: "running", jobId: submitted.job.job_id, error: null, report: null });
    } catch (err) {
      setState({ status: "failed", jobId: null, error: describeError(err), report: null });
    }
  };

  return (
    <div className="mb-2 flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <button
          type="button"
          className={buttonClass}
          onClick={() => void rescore()}
          disabled={!event.session || state.status === "submitting" || state.status === "running"}
          title={event.session ? undefined : "This event has no session recorded — cannot replay it."}
        >
          {state.status === "running" || state.status === "submitting"
            ? "re-scoring…"
            : "re-score this window"}
        </button>
        {state.status === "failed" && (
          <span className="text-[0.78rem] text-bad">{state.error}</span>
        )}
      </div>
      {state.status === "succeeded" && state.report && (
        <RescoreReport report={state.report} />
      )}
    </div>
  );
}

function RescoreReport({ report }: { report: ReplayWindowReport }) {
  const { isolation_forest: ifBlock, lstm_autoencoder: lstmBlock } = report;
  const lastIf = ifBlock.scores.at(-1) ?? null;
  const lastLstm = lstmBlock.windows.at(-1) ?? null;
  return (
    <div className="rounded-[8px] border border-border-soft bg-black/20 p-2.5 text-[0.78rem]">
      {report.warnings.length > 0 && (
        <p className="m-0 mb-2 text-warn">{report.warnings.join(" · ")}</p>
      )}
      <div className="grid grid-cols-2 gap-2 max-[900px]:grid-cols-1">
        <DetectorResult
          name="Isolation Forest"
          loaded={ifBlock.loaded}
          fired={ifBlock.anomalies.length > 0}
          scored={ifBlock.rows_scored}
          scoredUnit="rows"
          score={lastIf?.score ?? null}
          threshold={null}
        />
        <DetectorResult
          name="LSTM autoencoder"
          loaded={lstmBlock.loaded}
          fired={lstmBlock.anomalies.length > 0}
          scored={lstmBlock.windows_scored}
          scoredUnit="windows"
          score={lastLstm?.global_score ?? null}
          threshold={lastLstm?.global_threshold ?? null}
        />
      </div>
    </div>
  );
}

function DetectorResult({
  name,
  loaded,
  fired,
  scored,
  scoredUnit,
  score,
  threshold,
}: {
  name: string;
  loaded: boolean;
  fired: boolean;
  scored: number;
  scoredUnit: string;
  score: number | null;
  threshold: number | null;
}) {
  if (!loaded) {
    return (
      <div>
        <b className="text-fg">{name}</b>
        <p className={placeholderClass}>model not loaded for this replay.</p>
      </div>
    );
  }
  return (
    <div>
      <div className="flex items-center justify-between gap-2">
        <b className="text-fg">{name}</b>
        <span className={cn(badgeClass, fired ? "border-bad/60 text-bad" : "border-ok/60 text-ok")}>
          {fired ? "fired" : "quiet"}
        </span>
      </div>
      <p className="m-0 mt-1 text-fg-dim">
        {scored} {scoredUnit} scored
        {score !== null && ` · last score ${score.toFixed(3)}`}
        {threshold !== null && ` / threshold ${threshold.toFixed(3)}`}
      </p>
    </div>
  );
}

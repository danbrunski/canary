import { useCallback, useState } from "react";

import { getTrainingRuns, submitJob } from "../api/http";
import { Panel } from "../components/Panel";
import { Sparkline } from "../components/Sparkline";
import type { CompanionSettings } from "../hooks/useCompanionSettings";
import { useControlResource } from "../hooks/useControlResource";
import {
  buttonClass,
  cn,
  inputClass,
  noticeClass,
  placeholderClass,
  selectClass,
} from "../styles";
import type { TrainingRun } from "../types";

interface TrainingPanelProps {
  token: string;
  companion: CompanionSettings;
}

const PUBLISH_STYLE: Record<string, string> = {
  active: "text-ok",
  published: "text-accent-2",
  unpublished: "text-fg-faint",
  rolled_back: "text-bad",
};

const JOB_KINDS = ["retrain_if", "retrain_lstm", "retrain_all", "refit_behavior_models"];
type TrainingTarget = "pi" | "local";

/** Training dashboard (Task 6.22): run history + model quality from the
 *  training-run ledger (Task 6.21). Loss curve, threshold band, held-out
 *  FP rate and publish status per run. */
export function TrainingPanel({ token, companion }: TrainingPanelProps) {
  const [target, setTarget] = useState<TrainingTarget>("local");
  const [jobKind, setJobKind] = useState("retrain_if");
  const [jobDays, setJobDays] = useState("14");
  const [useAllSessions, setUseAllSessions] = useState(false);
  const [device, setDevice] = useState("cpu");
  const [notice, setNotice] = useState<{ kind: "ok" | "error" | "busy"; text: string } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const historyBase = target === "local" ? companion.baseUrl : undefined;
  const historyToken = target === "local" ? companion.token : token;
  const historyEnabled = target === "local" ? Boolean(companion.baseUrl) : Boolean(token);
  const fetcher = useCallback(
    (signal: AbortSignal) => getTrainingRuns(historyToken, signal, historyBase),
    [historyBase, historyToken],
  );
  const { data, error, loading, refresh } = useControlResource(fetcher, {
    enabled: historyEnabled,
    intervalMs: 15000,
  });

  const startTraining = async () => {
    const base = target === "local" ? companion.baseUrl : undefined;
    const jobToken = target === "local" ? companion.token : token;
    if (target === "local" && !companion.baseUrl) {
      setNotice({ kind: "error", text: "Enter the companion API URL." });
      return;
    }
    if (target === "pi" && !token) {
      setNotice({ kind: "error", text: "Enter the Pi control token first." });
      return;
    }
    const params: Record<string, unknown> = {};
    if (useAllSessions) {
      params.all = true;
    } else if (jobDays.trim()) {
      params.days = Number(jobDays);
    }
    if (jobKind.includes("lstm")) {
      params.device = device || "cpu";
    }
    setBusy(true);
    setNotice({ kind: "busy", text: "Submitting training job." });
    try {
      const response = await submitJob(jobToken, jobKind, params, base);
      setNotice({
        kind: "ok",
        text: `${response.job.kind} queued on ${target === "local" ? "laptop" : "Pi"}.`,
      });
      refresh();
    } catch (err) {
      setNotice({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  // Newest first.
  const runs = [...(data?.runs ?? [])].reverse();

  return (
    <Panel title="Training" count={data ? `${data.count} runs` : undefined}>
      <div className="mb-3 grid grid-cols-[auto_1fr_auto_auto_auto] items-end gap-2 max-[1100px]:grid-cols-1">
        <label className="flex flex-col gap-1 text-[0.75rem] text-fg-dim">
          Target
          <select
            className={selectClass}
            value={target}
            onChange={(event) => setTarget(event.target.value as TrainingTarget)}
          >
            <option value="local">Run on this laptop</option>
            <option value="pi">Run on Pi</option>
          </select>
        </label>
        {target === "local" ? (
          <label className="flex flex-col gap-1 text-[0.75rem] text-fg-dim">
            Companion API
            <input
              className={inputClass}
              value={companion.baseUrl}
              onChange={(event) => companion.setBaseUrl(event.target.value)}
              placeholder="http://127.0.0.1:8765"
            />
          </label>
        ) : (
          <div className="min-h-9 text-[0.75rem] text-fg-dim">
            {token ? "Pi control token present" : "Pi control token missing"}
          </div>
        )}
        <select
          className={selectClass}
          value={jobKind}
          onChange={(event) => setJobKind(event.target.value)}
        >
          {JOB_KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {kind}
            </option>
          ))}
        </select>
        <input
          className={inputClass}
          value={jobDays}
          onChange={(event) => setJobDays(event.target.value)}
          disabled={useAllSessions}
          inputMode="numeric"
          placeholder="days"
        />
        <button className={buttonClass} type="button" onClick={startTraining} disabled={busy}>
          Start
        </button>
      </div>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <label className="flex min-h-9 flex-row items-center gap-2 text-[0.75rem] text-fg-dim">
          <input
            type="checkbox"
            checked={useAllSessions}
            onChange={(event) => setUseAllSessions(event.target.checked)}
          />
          all sessions
        </label>
        {jobKind.includes("lstm") && (
          <label className="flex items-center gap-2 text-[0.75rem] text-fg-dim">
            device
            <input
              className={cn(inputClass, "w-24")}
              value={device}
              onChange={(event) => setDevice(event.target.value)}
              placeholder="cpu"
            />
          </label>
        )}
      </div>
      {target === "local" && (
        <label className="mb-3 flex max-w-md flex-col gap-1 text-[0.75rem] text-fg-dim">
          Local token
          <input
            className={inputClass}
            type="password"
            value={companion.token}
            onChange={(event) => companion.setToken(event.target.value)}
            placeholder="optional"
            autoComplete="off"
          />
        </label>
      )}
      {notice && <div className={cn(noticeClass(notice.kind), "mb-3")}>{notice.text}</div>}
      {!historyEnabled ? (
        <p className={placeholderClass}>
          {target === "local"
            ? "Enter the companion API URL to read laptop training history."
            : "Enter the Pi control token to read Pi training history."}
        </p>
      ) : loading ? (
        <p className={placeholderClass}>loading runs…</p>
      ) : error || !data ? (
        <p className={cn(placeholderClass, "text-bad")}>{error ?? "no data"}</p>
      ) : runs.length === 0 ? (
        <p className={placeholderClass}>No training runs recorded yet.</p>
      ) : (
        <div className="flex max-h-[34rem] flex-col gap-2 overflow-y-auto">
          {runs.map((run) => (
            <RunCard key={run.run_id} run={run} />
          ))}
        </div>
      )}
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

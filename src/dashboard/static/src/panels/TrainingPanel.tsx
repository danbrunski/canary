import { useCallback, useMemo, useState } from "react";

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
  const comparisons = useMemo(() => buildComparisons(data?.runs ?? []), [data]);

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
            <RunCard
              key={run.run_id}
              run={run}
              comparison={comparisons.get(run.run_id) ?? null}
            />
          ))}
        </div>
      )}
    </Panel>
  );
}

function RunCard({
  run,
  comparison,
}: {
  run: TrainingRun;
  comparison: RunComparison | null;
}) {
  const loss = numberArray(run.metrics.loss_curve);
  const quality = runQuality(run);

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
        {quality.fpRate !== null && (
          <span title="held-out false-positive rate">
            FP {(quality.fpRate * 100).toFixed(1)}%
          </span>
        )}
        {quality.alertsPerHour !== null && (
          <span>{quality.alertsPerHour.toFixed(2)} alerts/hr</span>
        )}
        {quality.meetsBudget !== null && (
          <span className={quality.meetsBudget ? "text-ok" : "text-warn"}>
            {quality.meetsBudget ? "budget ok" : "over budget"}
          </span>
        )}
        <time className="font-num">{run.created_at.slice(0, 19)}</time>
      </div>
      {quality.hasQuality && (
        <RunQualityView quality={quality} comparison={comparison} />
      )}
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

function RunQualityView({
  quality,
  comparison,
}: {
  quality: RunQuality;
  comparison: RunComparison | null;
}) {
  return (
    <div className="mt-3 flex flex-col gap-2">
      <div className="grid grid-cols-5 gap-2 max-[1200px]:grid-cols-2 max-[700px]:grid-cols-1">
        <QualityMetric label="FP" value={formatPercent(quality.fpRate)} />
        <QualityMetric
          label="alerts/hr"
          value={quality.alertsPerHour === null ? "-" : quality.alertsPerHour.toFixed(2)}
          tone={(quality.alertsPerHour ?? 0) > 1 ? "warn" : "ok"}
        />
        <QualityMetric label="precision" value={formatPercent(quality.precision)} />
        <QualityMetric label="recall" value={formatPercent(quality.recall)} />
        <QualityMetric
          label="delta"
          value={comparisonText(comparison)}
          tone={comparisonTone(comparison)}
        />
      </div>
      {quality.scoreDistribution && (
        <ScoreDistributionStrip
          distribution={quality.scoreDistribution}
          threshold={quality.threshold}
        />
      )}
      {quality.kSweep.length > 0 && <KSweepStrip points={quality.kSweep} />}
      {quality.slices.length > 0 && <ValidationSlices rows={quality.slices} />}
    </div>
  );
}

function QualityMetric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "ok" | "warn" | "bad";
}) {
  return (
    <div className="rounded-[8px] border border-border-soft bg-black/20 p-2">
      <div className="text-[0.6rem] uppercase tracking-[0.12em] text-fg-faint">
        {label}
      </div>
      <div className={cn("mt-1 font-num text-[0.9rem] tabular-nums", toneClass(tone))}>
        {value}
      </div>
    </div>
  );
}

function ScoreDistributionStrip({
  distribution,
  threshold,
}: {
  distribution: DistributionView;
  threshold: number | null;
}) {
  const span = distribution.max - distribution.min || 1;
  const pct = (value: number | null) =>
    value === null ? null : Math.max(0, Math.min(100, ((value - distribution.min) / span) * 100));
  const median = pct(distribution.median);
  const p95 = pct(distribution.p95);
  const thresh = pct(threshold ?? distribution.threshold);

  return (
    <div className="rounded-[8px] border border-border-soft bg-black/20 p-2">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
        <span>score distribution</span>
        <span className="font-num normal-case tracking-normal text-fg-dim">
          {formatCompact(distribution.min)} - {formatCompact(distribution.max)}
        </span>
      </div>
      <div className="relative h-7 rounded-full bg-elev-3">
        {median !== null && (
          <Marker left={median} label="median" className="bg-accent-2" />
        )}
        {p95 !== null && <Marker left={p95} label="p95" className="bg-warn" />}
        {thresh !== null && <Marker left={thresh} label="threshold" className="bg-bad" />}
      </div>
    </div>
  );
}

function Marker({
  left,
  label,
  className,
}: {
  left: number;
  label: string;
  className: string;
}) {
  return (
    <span
      className={cn("absolute top-1 h-5 w-1 rounded-full", className)}
      style={{ left: `calc(${left}% - 2px)` }}
      title={label}
    />
  );
}

function KSweepStrip({ points }: { points: KSweepPoint[] }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {points.slice(0, 8).map((point) => (
        <span
          key={point.k}
          className={cn(
            "rounded-full border px-2 py-0.5 font-num text-[0.66rem] tabular-nums",
            point.meetsBudget
              ? "border-ok/40 text-ok"
              : "border-warn/40 text-warn",
          )}
          title={`${point.alertsPerHour.toFixed(2)} alerts/hr`}
        >
          k{formatCompact(point.k)} {point.alertsPerHour.toFixed(2)}/hr
        </span>
      ))}
    </div>
  );
}

function ValidationSlices({ rows }: { rows: ValidationSlice[] }) {
  return (
    <div className="grid grid-cols-2 gap-1.5 max-[900px]:grid-cols-1">
      {rows.slice(0, 6).map((row) => (
        <div
          key={row.label}
          className="grid grid-cols-[minmax(5rem,1fr)_repeat(3,4rem)] gap-2 rounded-[6px] bg-black/20 px-2 py-1 text-[0.68rem]"
        >
          <span className="truncate text-fg-dim" title={row.label}>
            {row.label}
          </span>
          <span className="font-num text-fg">P {formatPercent(row.precision)}</span>
          <span className="font-num text-fg">R {formatPercent(row.recall)}</span>
          <span className="font-num text-fg">F1 {formatPercent(row.f1)}</span>
        </div>
      ))}
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

interface RunQuality {
  hasQuality: boolean;
  fpRate: number | null;
  alertsPerHour: number | null;
  meetsBudget: boolean | null;
  precision: number | null;
  recall: number | null;
  f1: number | null;
  threshold: number | null;
  scoreDistribution: DistributionView | null;
  kSweep: KSweepPoint[];
  slices: ValidationSlice[];
}

interface DistributionView {
  min: number;
  max: number;
  median: number | null;
  p95: number | null;
  threshold: number | null;
}

interface KSweepPoint {
  k: number;
  alertsPerHour: number;
  meetsBudget: boolean;
}

interface ValidationSlice {
  label: string;
  precision: number | null;
  recall: number | null;
  f1: number | null;
}

interface RunComparison {
  previousRunId: string;
  alertsDelta: number | null;
  fpDelta: number | null;
}

function runQuality(run: TrainingRun): RunQuality {
  const metrics = run.metrics;
  const manifestExtras = record(metrics.manifest_extras);
  const fp = record(metrics.false_positive_benchmark);
  const validation = record(metrics.validation) ?? record(manifestExtras?.validation);
  const overall = record(validation?.overall) ?? record(validation?.metrics);
  const fpGlobal = record(fp?.global);
  const distribution =
    distributionView(record(validation?.score_distribution)) ??
    distributionView(record(fpGlobal?.held_out_error_distribution)) ??
    distributionView(record(metrics.score_distribution));

  const alertsPerHour = numberOrNull(fp?.alerts_per_hour);
  const fpRate =
    numberOrNull(fp?.false_positive_rate) ??
    numberOrNull(overall?.false_positive_rate);
  const meetsBudget =
    typeof fp?.meets_v1_budget === "boolean"
      ? fp.meets_v1_budget
      : alertsPerHour === null
        ? null
        : alertsPerHour <= 1;
  const precision = numberOrNull(overall?.precision);
  const recall = numberOrNull(overall?.recall);
  const f1 = numberOrNull(overall?.f1);
  const calibratedThreshold =
    threshold(run) ?? numberOrNull(fpGlobal?.threshold) ?? distribution?.threshold ?? null;
  const kSweep = kSweepPoints(fp?.k_sweep);
  const slices = validationSlices(validation);
  const hasQuality =
    fpRate !== null ||
    alertsPerHour !== null ||
    precision !== null ||
    recall !== null ||
    distribution !== null ||
    kSweep.length > 0 ||
    slices.length > 0;

  return {
    hasQuality,
    fpRate,
    alertsPerHour,
    meetsBudget,
    precision,
    recall,
    f1,
    threshold: calibratedThreshold,
    scoreDistribution: distribution,
    kSweep,
    slices,
  };
}

function buildComparisons(runs: TrainingRun[]): Map<string, RunComparison> {
  const out = new Map<string, RunComparison>();
  const previousByType = new Map<string, TrainingRun>();
  const ordered = [...runs].sort(
    (a, b) => Date.parse(a.created_at) - Date.parse(b.created_at),
  );
  for (const run of ordered) {
    const previous = previousByType.get(run.model_type);
    if (previous) {
      const currentQuality = runQuality(run);
      const previousQuality = runQuality(previous);
      out.set(run.run_id, {
        previousRunId: previous.run_id,
        alertsDelta: delta(currentQuality.alertsPerHour, previousQuality.alertsPerHour),
        fpDelta: delta(currentQuality.fpRate, previousQuality.fpRate),
      });
    }
    previousByType.set(run.model_type, run);
  }
  return out;
}

function comparisonText(comparison: RunComparison | null): string {
  if (!comparison) return "new";
  if (comparison.alertsDelta !== null) {
    return `${formatSigned(comparison.alertsDelta)}/hr`;
  }
  if (comparison.fpDelta !== null) {
    return formatSignedPercent(comparison.fpDelta);
  }
  return `vs ${comparison.previousRunId}`;
}

function comparisonTone(comparison: RunComparison | null): "ok" | "warn" | undefined {
  if (!comparison) return undefined;
  const deltaValue = comparison.alertsDelta ?? comparison.fpDelta;
  if (deltaValue === null) return undefined;
  if (deltaValue < 0) return "ok";
  if (deltaValue > 0) return "warn";
  return undefined;
}

function distributionView(value: Record<string, unknown> | null): DistributionView | null {
  if (!value) return null;
  const min = numberOrNull(value.min) ?? numberOrNull(value.minimum);
  const max = numberOrNull(value.max) ?? numberOrNull(value.maximum);
  if (min === null || max === null || min === max) return null;
  return {
    min,
    max,
    median: numberOrNull(value.median) ?? numberOrNull(value.p50),
    p95: numberOrNull(value.q95) ?? numberOrNull(value.p95),
    threshold: numberOrNull(value.threshold),
  };
}

function kSweepPoints(value: unknown): KSweepPoint[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((raw) => {
      const item = record(raw);
      if (!item) return null;
      const k = numberOrNull(item.k);
      const alertsPerHour = numberOrNull(item.alerts_per_hour);
      if (k === null || alertsPerHour === null) return null;
      return {
        k,
        alertsPerHour,
        meetsBudget: item.meets_v1_budget === true,
      };
    })
    .filter((point): point is KSweepPoint => point !== null)
    .sort((a, b) => a.k - b.k);
}

function validationSlices(validation: Record<string, unknown> | null): ValidationSlice[] {
  if (!validation) return [];
  const raw =
    arrayOrNull(validation.by_fault_type) ??
    arrayOrNull(validation.fault_types) ??
    arrayOrNull(validation.synthetic_faults) ??
    arrayOrNull(validation.modes) ??
    objectValues(validation.modes);
  if (!raw) return [];
  return raw
    .map((item) => {
      const row = record(item);
      if (!row) return null;
      const metrics = record(row.metrics) ?? row;
      const label = String(row.fault_type ?? row.kind ?? row.mode ?? row.name ?? "slice");
      return {
        label,
        precision: numberOrNull(metrics.precision),
        recall: numberOrNull(metrics.recall),
        f1: numberOrNull(metrics.f1),
      };
    })
    .filter((row): row is ValidationSlice => row !== null);
}

function objectValues(value: unknown): unknown[] | null {
  const obj = record(value);
  return obj ? Object.values(obj) : null;
}

function arrayOrNull(value: unknown): unknown[] | null {
  return Array.isArray(value) ? value : null;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function delta(current: number | null, previous: number | null): number | null {
  return current === null || previous === null ? null : current - previous;
}

function formatPercent(value: number | null): string {
  return value === null ? "-" : `${(value * 100).toFixed(1)}%`;
}

function formatSigned(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}

function formatSignedPercent(value: number): string {
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;
}

function formatCompact(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 100) return value.toFixed(0);
  if (abs >= 10) return value.toFixed(1);
  return value.toFixed(2);
}

function toneClass(tone: "ok" | "warn" | "bad" | undefined): string {
  if (tone === "ok") return "text-ok";
  if (tone === "warn") return "text-warn";
  if (tone === "bad") return "text-bad";
  return "";
}

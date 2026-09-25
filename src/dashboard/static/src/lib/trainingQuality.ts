// Training-run quality shaping: collapse the loosely-typed training-run
// ledger metrics (FP benchmark, validation slices, score distribution,
// k-sweep) into the structures the run cards render, plus run-over-run
// comparison. Split out of TrainingPanel so the logic-heavy part is
// React-free and unit-testable.

import type { TrainingRun } from "../types";
import { formatSigned, formatSignedPercent } from "../util/format";
import { arrayOrNull, numberOrNull, objectValues, record, scalarText } from "../util/json";

export interface RunQuality {
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

export interface DistributionView {
  min: number;
  max: number;
  median: number | null;
  p95: number | null;
  threshold: number | null;
}

export interface KSweepPoint {
  k: number;
  alertsPerHour: number;
  meetsBudget: boolean;
}

export interface ValidationSlice {
  label: string;
  precision: number | null;
  recall: number | null;
  f1: number | null;
}

export interface RunComparison {
  previousRunId: string;
  alertsDelta: number | null;
  fpDelta: number | null;
}

/** LSTM calibrated global threshold from the manifest metrics, if present. */
export function threshold(run: TrainingRun): number | null {
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

export function runQuality(run: TrainingRun): RunQuality {
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
    numberOrNull(fp?.false_positive_rate) ?? numberOrNull(overall?.false_positive_rate);
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

export function buildComparisons(runs: TrainingRun[]): Map<string, RunComparison> {
  const out = new Map<string, RunComparison>();
  const previousByType = new Map<string, TrainingRun>();
  const ordered = [...runs].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
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

export function comparisonText(comparison: RunComparison | null): string {
  if (!comparison) return "new";
  if (comparison.alertsDelta !== null) {
    return `${formatSigned(comparison.alertsDelta)}/hr`;
  }
  if (comparison.fpDelta !== null) {
    return formatSignedPercent(comparison.fpDelta);
  }
  return `vs ${comparison.previousRunId}`;
}

export function comparisonTone(comparison: RunComparison | null): "ok" | "warn" | undefined {
  if (!comparison) return undefined;
  const deltaValue = comparison.alertsDelta ?? comparison.fpDelta;
  if (deltaValue === null) return undefined;
  if (deltaValue < 0) return "ok";
  if (deltaValue > 0) return "warn";
  return undefined;
}

export function distributionView(value: Record<string, unknown> | null): DistributionView | null {
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

export function kSweepPoints(value: unknown): KSweepPoint[] {
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

export function validationSlices(validation: Record<string, unknown> | null): ValidationSlice[] {
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
      const label = scalarText(row.fault_type ?? row.kind ?? row.mode ?? row.name) ?? "slice";
      return {
        label,
        precision: numberOrNull(metrics.precision),
        recall: numberOrNull(metrics.recall),
        f1: numberOrNull(metrics.f1),
      };
    })
    .filter((row): row is ValidationSlice => row !== null);
}

export function delta(current: number | null, previous: number | null): number | null {
  return current === null || previous === null ? null : current - previous;
}

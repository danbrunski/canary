import { MeterBar, MetricTile } from "../components/MetricTile";
import { Panel } from "../components/Panel";
import {
  cn,
  definitionListClass,
  definitionRowClass,
  definitionTermClass,
  definitionValueClass,
  placeholderClass,
} from "../styles";
import type { StatusHealth, StatusResponse } from "../types";
import { formatPercent, toneClass } from "../util/format";
import { numberMap, numberOr, numberOrNull, stringArray } from "../util/json";

interface ModelHealthPanelProps {
  status: StatusResponse | null;
  statusError: Error | null;
}

export function ModelHealthPanel({ status, statusError }: ModelHealthPanelProps) {
  const health = status?.health ?? null;

  return (
    <Panel title="Drift & Inference Health">
      {statusError && (
        <div className="mb-2 rounded-[8px] border border-bad/40 bg-bad/10 px-2.5 py-1.5 text-[0.82rem] text-bad">
          /status unreachable: {statusError.message}
        </div>
      )}
      {!status ? (
        <p className={placeholderClass}>loading status…</p>
      ) : !health ? (
        <p className={placeholderClass}>health monitor is not wired.</p>
      ) : (
        <HealthBody health={health} />
      )}
    </Panel>
  );
}

function HealthBody({ health }: { health: StatusHealth }) {
  const featureDrift = featureDriftView(health.feature_drift);
  const reconDrift = reconstructionDriftView(health.reconstruction_error_drift);

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-4 gap-2 max-[1200px]:grid-cols-2 max-[700px]:grid-cols-1">
        <MetricTile
          label="latency mean"
          value={formatMs(health.inference_latency_ms.mean)}
          tone={latencyTone(health.inference_latency_ms.mean)}
        />
        <MetricTile
          label="latency p95"
          value={formatMs(health.inference_latency_ms.p95)}
          tone={latencyTone(health.inference_latency_ms.p95)}
        />
        <MetricTile
          label="missing features"
          value={formatPercent(health.missing_feature_rate, 0)}
          tone={(health.missing_feature_rate ?? 0) > 0.25 ? "warn" : "ok"}
        />
        <MetricTile
          label="freshness"
          value={
            health.data_freshness_seconds === null
              ? "-"
              : `${health.data_freshness_seconds.toFixed(1)}s`
          }
          tone={(health.data_freshness_seconds ?? 0) > 10 ? "warn" : "ok"}
        />
      </div>

      <div className="grid grid-cols-3 gap-3 max-[1200px]:grid-cols-1">
        <Section title="Feature Drift">
          {featureDrift === null ? (
            <p className={placeholderClass}>no feature drift snapshot.</p>
          ) : (
            <div className="flex flex-col gap-2">
              <dl className={definitionListClass}>
                <InfoRow label="max sigma" value={featureDrift.maxSigma.toFixed(2)} />
                <InfoRow
                  label="schema"
                  value={featureDrift.schemaMismatch ? "mismatch" : "aligned"}
                  tone={featureDrift.schemaMismatch ? "bad" : "ok"}
                />
                <InfoRow
                  label="missing"
                  value={String(featureDrift.missingFeatures.length)}
                  tone={featureDrift.missingFeatures.length > 0 ? "warn" : "ok"}
                />
              </dl>
              <RankedBars
                values={featureDrift.topFeatures}
                valueSuffix="sigma"
                dangerAt={3}
              />
              {featureDrift.missingFeatures.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {featureDrift.missingFeatures.slice(0, 8).map((feature) => (
                    <span
                      key={feature}
                      className="rounded-full border border-warn/40 px-2 py-0.5 text-[0.66rem] text-warn"
                    >
                      {feature}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}
        </Section>

        <Section title="Reconstruction Drift">
          {reconDrift === null ? (
            <p className={placeholderClass}>no reconstruction drift snapshot.</p>
          ) : (
            <dl className={definitionListClass}>
              <InfoRow
                label="live mean"
                value={formatNumberOrDash(reconDrift.liveMeanError)}
              />
              <InfoRow
                label="train ratio"
                value={reconDrift.ratio === null ? "-" : `${reconDrift.ratio.toFixed(2)}x`}
                tone={(reconDrift.ratio ?? 0) > 1.5 ? "warn" : "ok"}
              />
              <InfoRow
                label="sigma"
                value={formatNumberOrDash(reconDrift.sigmaAboveTrain)}
                tone={(reconDrift.sigmaAboveTrain ?? 0) > 3 ? "bad" : "ok"}
              />
            </dl>
          )}
        </Section>

        <Section title="System / Swaps">
          <dl className={definitionListClass}>
            <InfoRow
              label="load"
              value={formatPercent(health.system.load_per_cpu, 0)}
              tone={(health.system.load_per_cpu ?? 0) > 0.85 ? "warn" : "ok"}
            />
            <InfoRow
              label="memory"
              value={formatPercent(health.system.mem_used_fraction, 0)}
              tone={(health.system.mem_used_fraction ?? 0) > 0.85 ? "warn" : "ok"}
            />
            <InfoRow
              label="mem total"
              value={
                health.system.mem_total_mb === null
                  ? "-"
                  : `${health.system.mem_total_mb.toFixed(0)} MB`
              }
            />
            {Object.entries(health.model_swaps).map(([kind, count]) => (
              <InfoRow
                key={kind}
                label={kind}
                value={String(count)}
                tone={kind === "rejected" && count > 0 ? "warn" : undefined}
              />
            ))}
          </dl>
        </Section>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-[8px] border border-border-soft bg-black/20 p-3">
      <h3 className="mb-2 mt-0 text-[0.72rem] font-bold uppercase tracking-[0.12em] text-fg-dim">
        {title}
      </h3>
      {children}
    </section>
  );
}

function InfoRow({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "ok" | "warn" | "bad";
}) {
  return (
    <div className={definitionRowClass}>
      <dt className={definitionTermClass}>{label}</dt>
      <dd className={cn(definitionValueClass, toneClass(tone))}>{value}</dd>
    </div>
  );
}

function RankedBars({
  values,
  valueSuffix,
  dangerAt,
}: {
  values: Array<[string, number]>;
  valueSuffix: string;
  dangerAt: number;
}) {
  if (values.length === 0) return null;
  const max = Math.max(1, ...values.map(([, value]) => value));
  return (
    <div className="flex flex-col gap-1.5">
      {values.slice(0, 8).map(([name, value]) => (
        <div key={name} className="grid grid-cols-[minmax(6rem,1fr)_5rem] gap-2">
          <div className="min-w-0">
            <div className="mb-0.5 truncate text-[0.66rem] text-fg-dim" title={name}>
              {name}
            </div>
            <MeterBar
              value={value}
              max={max}
              colorClass={
                value >= dangerAt
                  ? "bg-bad"
                  : value >= dangerAt * 0.7
                    ? "bg-warn"
                    : "bg-accent-2"
              }
            />
          </div>
          <div className="self-end text-right font-num text-[0.66rem] text-fg-dim">
            {value.toFixed(2)} {valueSuffix}
          </div>
        </div>
      ))}
    </div>
  );
}

interface FeatureDriftView {
  maxSigma: number;
  schemaMismatch: boolean;
  missingFeatures: string[];
  topFeatures: Array<[string, number]>;
}

function featureDriftView(value: Record<string, unknown> | undefined): FeatureDriftView | null {
  if (!value) return null;
  const perFeature = numberMap(value.per_feature_sigma) ?? {};
  return {
    maxSigma: numberOr(value.max_sigma, 0),
    schemaMismatch: value.schema_mismatch === true,
    missingFeatures: stringArray(value.missing_features),
    topFeatures: Object.entries(perFeature).sort((a, b) => b[1] - a[1]),
  };
}

interface ReconstructionDriftView {
  liveMeanError: number | null;
  ratio: number | null;
  sigmaAboveTrain: number | null;
}

function reconstructionDriftView(
  value: Record<string, unknown> | undefined,
): ReconstructionDriftView | null {
  if (!value) return null;
  return {
    liveMeanError: numberOrNull(value.live_mean_error),
    ratio: numberOrNull(value.ratio),
    sigmaAboveTrain: numberOrNull(value.sigma_above_train),
  };
}

function formatMs(value: number | null): string {
  return value === null ? "idle" : `${value.toFixed(1)} ms`;
}

function formatNumberOrDash(value: number | null): string {
  return value === null ? "-" : value.toFixed(4);
}

function latencyTone(value: number | null): "ok" | "warn" | "bad" {
  if (value === null) return "ok";
  if (value > 500) return "bad";
  if (value > 250) return "warn";
  return "ok";
}


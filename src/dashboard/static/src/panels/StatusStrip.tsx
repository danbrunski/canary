import { useEffect, useState } from "react";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import {
  cn,
  statusItemClass,
  statusLabelClass,
  statusStripClass,
  statusValueClass,
} from "../styles";
import type { AnomalyEvent, StatusResponse } from "../types";
import { STALE_THRESHOLD_MS, computeFreshness } from "../util/freshness";

interface StatusStripProps {
  status: StatusResponse | null;
  channels: Map<string, ChannelSnapshot>;
  events: AnomalyEvent[];
}

/** The always-visible "is everything OK" spine, pinned under the header in
 *  both Drive and Workbench. Pulls the few facts that matter at a glance —
 *  live data freshness, current anomaly state, and the model-health summary
 *  from `/status.health` (Task 6.27) — out of the panels where they were
 *  buried so the operator never has to go looking for them. */
export function StatusStrip({ status, channels, events }: StatusStripProps) {
  // Re-tick once a second so freshness / anomaly-age stay live without a
  // telemetry message arriving.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const freshnessMs = computeFreshness(channels, now);
  const stale = freshnessMs !== null && freshnessMs > STALE_THRESHOLD_MS;

  const recent = mostRecentEvent(events, now);
  const health = status?.health ?? null;
  const counters = status?.emitter_counters ?? null;

  return (
    <div className={statusStripClass} aria-label="System status">
      <StatusItem label="data">
        <span className={cn(statusValueClass, stale && "text-bad")}>
          {freshnessMs === null
            ? "—"
            : `${(freshnessMs / 1000).toFixed(1)}s${stale ? " stale" : ""}`}
        </span>
      </StatusItem>

      <StatusItem label="anomaly">
        {recent === null ? (
          <span className={cn(statusValueClass, "text-ok")}>nominal</span>
        ) : (
          <span
            className={cn(
              statusValueClass,
              recent.fresh ? "text-bad" : "text-warn",
            )}
          >
            {recent.fresh ? "● active" : "○ recent"} · {recent.label}
          </span>
        )}
      </StatusItem>

      {counters && (
        <StatusItem label="alerts">
          <span className={statusValueClass}>
            {counters.emitted} emitted
            {counters.suppressed_tuning > 0 &&
              ` · ${counters.suppressed_tuning} tuned-out`}
          </span>
        </StatusItem>
      )}

      {health && (
        <>
          <StatusItem label="inference">
            <span className={statusValueClass}>
              {health.inference_latency_ms.mean === null
                ? "idle"
                : `${health.inference_latency_ms.mean.toFixed(1)} ms`}
            </span>
          </StatusItem>
          <StatusItem label="missing feat">
            <span
              className={cn(
                statusValueClass,
                (health.missing_feature_rate ?? 0) > 0.25 && "text-warn",
              )}
            >
              {health.missing_feature_rate === null
                ? "—"
                : `${(health.missing_feature_rate * 100).toFixed(0)}%`}
            </span>
          </StatusItem>
          {health.system.load_per_cpu != null && (
            <StatusItem label="cpu">
              <span
                className={cn(
                  statusValueClass,
                  health.system.load_per_cpu > 0.85 && "text-warn",
                )}
              >
                {(health.system.load_per_cpu * 100).toFixed(0)}%
              </span>
            </StatusItem>
          )}
        </>
      )}

      <StatusItem label="model">
        <span className={statusValueClass}>
          {modelSummary(status)}
        </span>
      </StatusItem>
    </div>
  );
}

function StatusItem({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <span className={statusItemClass}>
      <span className={statusLabelClass}>{label}</span>
      {children}
    </span>
  );
}

interface RecentAnomaly {
  fresh: boolean; // within the active window
  label: string;
}

/** The freshest anomaly + whether it's "active" (last 60 s) vs just recent.
 *  Event timestamps are ISO-8601; unparseable ones are skipped rather than
 *  treated as "now". */
function mostRecentEvent(
  events: AnomalyEvent[],
  now: number,
): RecentAnomaly | null {
  for (const e of events) {
    const t = Date.parse(e.timestamp);
    if (Number.isNaN(t)) continue;
    const ageMs = now - t;
    const src = e.source === "lstm_autoencoder" ? "LSTM" : "IF";
    const mode = e.operating_mode ? ` (${e.operating_mode})` : "";
    return { fresh: ageMs >= 0 && ageMs < 60_000, label: `${src}${mode}` };
  }
  return null;
}

function modelSummary(status: StatusResponse | null): string {
  if (!status) return "—";
  const versions = Object.entries(status.model_versions).filter(
    ([, v]) => v != null,
  );
  if (versions.length === 0) return "none loaded";
  return versions.map(([k]) => k.replace("_autoencoder", "")).join(" · ");
}

import { useEffect, useState } from "react";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import {
  cn,
  statusItemClass,
  statusLabelClass,
  statusStripClass,
  statusValueClass,
} from "../styles";
import type {
  AcceptedBaselineOverrideSummary,
  AnomalyEvent,
  StatusResponse,
} from "../types";
import { STALE_THRESHOLD_MS, computeFreshness } from "../util/freshness";
import { useLocalStorageJson } from "../hooks/useLocalStorage";

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
  const overrides = status?.accepted_baseline_overrides ?? null;
  const [overridesOpen, setOverridesOpen] = useState(false);
  // Task 1.2 — the strip's own "what am I looking at" tell, independent of
  // the full-width ReplayBanner (which can be dismissed from view by
  // scrolling); this one line stays pinned under the header in both modes.
  const replaying = status?.replay?.running === true;
  const [acknowledgedCapture, setAcknowledgedCapture] = useLocalStorageJson<string | null>(
    "canary.lastCaptureAcknowledged.v1",
    null,
  );
  const endedCapture = status?.last_capture;
  const announceCapture = Boolean(
    endedCapture?.end_reason &&
    endedCapture.end_reason !== "operator_stop" &&
    endedCapture.session_dir !== acknowledgedCapture,
  );

  return (
    <div className={cn(statusStripClass, "relative")} aria-label="System status">
      {announceCapture && endedCapture && (
        <div className="flex items-center gap-2 border border-warn/60 bg-warn/10 px-2 py-1 text-xs text-warn">
          <span>{captureEndedMessage(endedCapture)}</span>
          <button
            type="button"
            className="underline"
            onClick={() => setAcknowledgedCapture(endedCapture.session_dir)}
          >
            acknowledge
          </button>
        </div>
      )}
      <StatusItem label="source">
        <span className={cn(statusValueClass, replaying && "text-warn")}>
          {replaying ? "replay" : "live"}
        </span>
      </StatusItem>

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

      {overrides && (
        <StatusItem label="overrides">
          <button
            type="button"
            className={cn(statusValueClass, "underline decoration-dotted")}
            aria-expanded={overridesOpen}
            onClick={() => setOverridesOpen((open) => !open)}
          >
            {overrides.active} active
            {overrides.overrides.some((o) => o.expired) && (
              <span className="text-warn"> · expired</span>
            )}
          </button>
          {overridesOpen && (
            <OverridesDropdown overrides={overrides.overrides} />
          )}
        </StatusItem>
      )}
    </div>
  );
}

function captureEndedMessage(capture: NonNullable<StatusResponse["last_capture"]>): string {
  const started = Date.parse(capture.started_at);
  const ended = capture.ended_at == null ? Number.NaN : Date.parse(capture.ended_at);
  const minutes = Number.isFinite(started) && Number.isFinite(ended)
    ? Math.max(0, Math.round((ended - started) / 60000))
    : null;
  const reason = capture.end_reason === "daemon_shutdown"
    ? "daemon stopped"
    : capture.end_reason === "interrupted"
      ? "daemon restarted or lost power"
      : capture.end_reason === "profile_switch"
        ? "profile switched"
        : "unknown reason";
  return `${capture.name} ended${minutes == null ? "" : ` after ${minutes} min`}: ${reason}`;
}

/** Expandable per-override provenance (2.3) — "N accepted-baseline
 *  overrides active" is always visible above; this is the detail behind
 *  it, anchored under the strip so it never displaces the always-visible
 *  spine layout. Expired overrides are flagged rather than hidden — the
 *  doc is explicit that a lapsed override needs re-confirmation, not
 *  silent continuation. */
function OverridesDropdown({
  overrides,
}: {
  overrides: AcceptedBaselineOverrideSummary[];
}) {
  if (overrides.length === 0) {
    return (
      <div className={overridesDropdownClass}>
        <p className="m-0 text-fg-faint">No accepted-baseline overrides declared.</p>
      </div>
    );
  }
  return (
    <div className={overridesDropdownClass}>
      {overrides.map((o) => (
        <div
          key={`${o.target}:${o.operating_mode ?? ""}`}
          className="flex flex-col gap-0.5 border-b border-border-soft py-1.5 last:border-b-0 last:pb-0"
        >
          <div className="flex items-center gap-2">
            <span className="font-num text-fg">{o.target}</span>
            {o.operating_mode && (
              <span className="text-fg-faint">({o.operating_mode})</span>
            )}
            {o.expired && (
              <span className="text-warn">needs re-confirmation</span>
            )}
          </div>
          <p className="m-0 text-fg-dim">{o.reason}</p>
          <p className="m-0 text-fg-faint">
            {o.declared_by} · declared {o.declared_at} · review by {o.review_by}
          </p>
        </div>
      ))}
    </div>
  );
}

const overridesDropdownClass = [
  "absolute",
  "left-0",
  "top-full",
  "z-10",
  "mt-1",
  "w-full",
  "max-w-[28rem]",
  "rounded-[8px]",
  "border",
  "border-border-soft",
  "bg-elev",
  "px-3",
  "py-2",
  "text-[0.78rem]",
  "shadow-lg",
].join(" ");

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

//: Last 60 s — something is firing right now ("● active").
const ANOMALY_ACTIVE_MS = 60_000;
//: Beyond this the strip reverts to "nominal". The event list (Anomalies
//: panel) keeps full history; this spine answers "anything wrong *now*?".
//: Without it the freshest event sticks forever — e.g. a pre-restart false
//: positive showing "○ recent · Fused (warmup)" long after the truck went
//: quiet (and, on a fresh page load, a day-old event from the log history).
const ANOMALY_RECENT_MS = 5 * 60_000;

/** The freshest *alert* + whether it's "active" (last 60 s) vs just recent.
 *  Evidence-role events (A06.5) are skipped.
 *  Returns null once the freshest event is older than the recent window, so the
 *  strip clears to "nominal". Event timestamps are ISO-8601; unparseable ones
 *  are skipped rather than treated as "now". */
function mostRecentEvent(
  events: AnomalyEvent[],
  now: number,
): RecentAnomaly | null {
  for (const e of events) {
    // A06.5: an evidence-role detector's event is stored and listed in the
    // Anomalies panel, but it is not an alert — it never lights this spine.
    if (e.role === "evidence") continue;
    const t = Date.parse(e.timestamp);
    if (Number.isNaN(t)) continue;
    const ageMs = now - t;
    if (ageMs >= ANOMALY_RECENT_MS) return null;
    const src =
      e.source === "lstm_autoencoder"
        ? "LSTM"
        : e.source === "fused"
          ? "Fused"
          : e.source === "behavior"
            ? "Behavior"
            : "IF";
    const mode = e.operating_mode ? ` (${e.operating_mode})` : "";
    return { fresh: ageMs >= 0 && ageMs < ANOMALY_ACTIVE_MS, label: `${src}${mode}` };
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

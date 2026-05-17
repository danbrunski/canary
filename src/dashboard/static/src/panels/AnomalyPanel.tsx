import { useMemo, useState } from "react";

import { Sparkline } from "../components/Sparkline";
import {
  badgeClass,
  cn,
  panelClass,
  panelHeaderClass,
  panelTitleClass,
  placeholderClass,
  selectClass,
} from "../styles";
import type { AnomalyEvent } from "../types";

interface AnomalyPanelProps {
  events: AnomalyEvent[];
}

/** Anomaly timeline (Task 5.10): the most recent score chart up top, then a
 *  filterable list of events. The chart is fixed-width historical — newest
 *  events on the right — so an operator at a glance can see "are we
 *  trending hotter?" without parsing timestamps. */
export function AnomalyPanel({ events }: AnomalyPanelProps) {
  const [sourceFilter, setSourceFilter] = useState<AnomalyEvent["source"] | "all">("all");
  const [modeFilter, setModeFilter] = useState<string | "all">("all");

  const visible = useMemo(() => {
    return events.filter((e) => {
      if (sourceFilter !== "all" && e.source !== sourceFilter) return false;
      if (modeFilter !== "all" && e.operating_mode !== modeFilter) return false;
      return true;
    });
  }, [events, sourceFilter, modeFilter]);

  // Last 60 events (oldest first), score axis
  const scoreSeries = useMemo(() => {
    return [...visible].reverse().slice(-60).map((e) => e.score);
  }, [visible]);

  const modes = useMemo(() => {
    const set = new Set<string>();
    for (const e of events) if (e.operating_mode) set.add(e.operating_mode);
    return [...set].sort();
  }, [events]);

  return (
    <section className={cn(panelClass, "col-span-2 max-[1100px]:col-span-1")}>
      <header className={panelHeaderClass}>
        <h2 className={panelTitleClass}>Anomalies</h2>
        <div className="flex gap-3 font-num text-[0.78rem] tabular-nums text-fg-dim">
          <label className="flex items-center gap-1.5">
            source{" "}
            <select
              className={cn(selectClass, "min-h-0 rounded py-0.5 pl-1.5 pr-6")}
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value as typeof sourceFilter)}
            >
              <option value="all">all</option>
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
      </header>
      {events.length === 0 ? (
        <p className={placeholderClass}>
          no anomalies yet — the daemon hasn't emitted any (or the inference
          path isn't wired). check /status's emitter_counters.
        </p>
      ) : (
        <>
          <div className="mb-2 flex flex-col gap-1">
            <Sparkline values={scoreSeries} width={520} height={64} stroke="#FFB84A" />
            <span className="text-[0.7rem] text-fg-dim">
              last {scoreSeries.length} scores
            </span>
          </div>
          <ol className="m-0 max-h-[22rem] list-none overflow-y-auto p-0">
            {visible.slice(0, 50).map((e) => (
              <li
                key={`${e.timestamp}-${e.source}-${e.model_version}`}
                className="flex flex-wrap items-baseline gap-2 border-b border-border py-1.5 text-[0.85rem] last:border-b-0"
              >
                <time className="font-num tabular-nums text-fg-dim">
                  {formatTimestamp(e.timestamp)}
                </time>
                <span
                  className={cn(
                    "rounded bg-elev-2 px-1.5 py-0.5 text-[0.7rem] uppercase tracking-[0.08em]",
                    e.source === "isolation_forest" && "text-accent",
                    e.source === "lstm_autoencoder" && "text-ok",
                  )}
                >
                  {e.source.replace("_", " ")}
                </span>
                {e.operating_mode && (
                  <span className="text-[0.75rem] text-fg-dim">{e.operating_mode}</span>
                )}
                <span className="font-num tabular-nums text-bad">
                  score {e.score.toFixed(3)}
                </span>
                {e.threshold !== null && (
                  <span className="font-num tabular-nums text-fg-dim">
                    / {e.threshold.toFixed(3)}
                  </span>
                )}
                {e.affected_channels.length > 0 && (
                  <span className="text-[0.78rem] text-fg-dim">
                    {e.affected_channels.slice(0, 3).join(", ")}
                    {e.affected_channels.length > 3 && "…"}
                  </span>
                )}
                {e.detail?.missing_features === true && (
                  <span className={cn(badgeClass, "border-warn bg-warn text-bg")}>
                    missing features
                  </span>
                )}
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString();
  } catch {
    return ts;
  }
}

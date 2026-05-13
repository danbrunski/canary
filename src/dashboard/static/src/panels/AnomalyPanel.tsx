import { useMemo, useState } from "react";

import { Sparkline } from "../components/Sparkline";
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
    <section className="panel anomaly-panel">
      <header>
        <h2>Anomalies</h2>
        <div className="filters">
          <label>
            source{" "}
            <select
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value as typeof sourceFilter)}
            >
              <option value="all">all</option>
              <option value="isolation_forest">isolation_forest</option>
              <option value="lstm_autoencoder">lstm_autoencoder</option>
            </select>
          </label>
          {modes.length > 0 && (
            <label>
              mode{" "}
              <select
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
        <p className="placeholder">
          no anomalies yet — the daemon hasn't emitted any (or the inference
          path isn't wired). check /status's emitter_counters.
        </p>
      ) : (
        <>
          <div className="score-chart">
            <Sparkline values={scoreSeries} width={520} height={64} stroke="#FFB84A" />
            <span className="score-label">last {scoreSeries.length} scores</span>
          </div>
          <ol className="event-list">
            {visible.slice(0, 50).map((e) => (
              <li key={`${e.timestamp}-${e.source}-${e.model_version}`}>
                <time>{formatTimestamp(e.timestamp)}</time>
                <span className={`source ${e.source}`}>{e.source.replace("_", " ")}</span>
                {e.operating_mode && <span className="mode">{e.operating_mode}</span>}
                <span className="score">score {e.score.toFixed(3)}</span>
                {e.threshold !== null && (
                  <span className="threshold">/ {e.threshold.toFixed(3)}</span>
                )}
                {e.affected_channels.length > 0 && (
                  <span className="channels">
                    {e.affected_channels.slice(0, 3).join(", ")}
                    {e.affected_channels.length > 3 && "…"}
                  </span>
                )}
                {e.detail?.missing_features === true && (
                  <span className="badge dropout">missing features</span>
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

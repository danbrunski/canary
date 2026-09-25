import { useCallback } from "react";

import { getTrends } from "../api/http";
import { Panel } from "../components/Panel";
import { Sparkline } from "../components/Sparkline";
import { useControlResource } from "../hooks/useControlResource";
import { cn, placeholderClass } from "../styles";
import type { StatTrend } from "../types";
import { formatNumber, toneClass } from "../util/format";
import { formatTimestamp } from "../util/time";

interface TrendsPanelProps {
  token: string;
}

/** Session-fingerprint drift (Task 4.5): per-stat sparkline over its
 *  session-ordered series, the frozen reference epoch's band, the
 *  Mann-Kendall trend verdict, and CUSUM alarm state (Task 4.4). Read-only —
 *  freezing/re-freezing the reference epoch is a deliberate operator action
 *  via `scripts/fingerprint.py freeze`, not something the dashboard drives. */
export function TrendsPanel({ token }: TrendsPanelProps) {
  const fetcher = useCallback((signal: AbortSignal) => getTrends(token, signal), [token]);
  const { data, error, loading } = useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: 30000,
  });

  if (!token) {
    return (
      <Panel title="Drift Trends">
        <p className={placeholderClass}>
          Enter the control token (Control panel) to view fingerprint trends.
        </p>
      </Panel>
    );
  }
  if (loading) {
    return (
      <Panel title="Drift Trends">
        <p className={placeholderClass}>loading trends…</p>
      </Panel>
    );
  }
  if (error || !data) {
    return (
      <Panel title="Drift Trends">
        <p className={cn(placeholderClass, "text-bad")}>{error ?? "no data"}</p>
      </Panel>
    );
  }
  if (!data.available) {
    return (
      <Panel title="Drift Trends">
        <p className={placeholderClass}>
          No reference epoch frozen yet — run <code>scripts/fingerprint.py freeze</code> once a
          healthy baseline of sessions exists.
        </p>
      </Panel>
    );
  }

  const stats = Object.values(data.stats).sort((a, b) => a.stat.localeCompare(b.stat));

  return (
    <Panel
      title="Drift Trends"
      count={
        data.frozen_at
          ? `frozen ${formatTimestamp(data.frozen_at)} · ${data.reference_sessions ?? 0} sessions`
          : undefined
      }
    >
      {stats.length === 0 ? (
        <p className={placeholderClass}>
          Reference epoch frozen, but no session has a fingerprint yet.
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {stats.map((trend) => (
            <StatRow key={trend.stat} trend={trend} />
          ))}
        </div>
      )}
    </Panel>
  );
}

function StatRow({ trend }: { trend: StatTrend }) {
  const alarm = trend.cusum?.alarm ?? false;
  const mkTrend = trend.mann_kendall?.trend ?? "no trend";
  const [bandLo, bandHi] = trend.reference_band;
  const sparkMin = Math.min(bandLo, ...trend.values);
  const sparkMax = Math.max(bandHi, ...trend.values);

  return (
    <div
      className={cn(
        "grid grid-cols-[1fr_auto] items-center gap-x-3 gap-y-1 border-b border-border-soft pb-2 last:border-0",
        alarm && "rounded-[6px] border border-bad/40 bg-bad/5 px-2 pt-1",
      )}
    >
      <div className="min-w-0">
        <div className="truncate text-[0.82rem] font-semibold text-fg">{trend.stat}</div>
        <div className="text-[0.72rem] text-fg-faint">
          latest {formatNumber(trend.latest_value)} · band [{formatNumber(bandLo)},{" "}
          {formatNumber(bandHi)}] · n={trend.n}
        </div>
      </div>
      <Sparkline
        values={trend.values}
        min={sparkMin}
        max={sparkMax}
        baseline={trend.reference_mean}
        stroke={alarm ? "#ef4444" : "#ffb547"}
      />
      <div className="col-span-2 flex items-center gap-3 text-[0.72rem]">
        <span className={mkTrend === "no trend" ? "text-fg-faint" : "text-fg-dim"}>
          trend: {mkTrend}
        </span>
        <span className={cn("font-semibold", toneClass(alarm ? "bad" : "ok"))}>
          {alarm ? `CUSUM ALARM (${trend.cusum?.direction ?? "?"})` : "CUSUM ok"}
        </span>
      </div>
    </div>
  );
}

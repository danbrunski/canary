import { useCallback, useState } from "react";

import { getMonitors } from "../api/http";
import { Panel } from "../components/Panel";
import { useControlResource } from "../hooks/useControlResource";
import { cn, placeholderClass } from "../styles";
import type { MonitorTestResultEntry } from "../types";
import { formatTimestamp } from "../util/time";

export interface MisfireCylinderRow {
  cylinder: string;
  count: number;
  /** ``count`` minus the drive-start baseline for this cylinder; 0 when the
   *  cylinder wasn't in the baseline snapshot (nothing to diff against yet). */
  delta: number;
}

/** Pure delta computation (Task 3.5's "delta-since-drive-start highlighted"),
 *  factored out so it's unit-testable without racing the panel's poll
 *  interval: ``baseline`` is the misfire-count snapshot captured when the
 *  panel first saw data; ``counts`` is the latest poll. A cylinder absent
 *  from ``baseline`` (the very first render) has ``delta = 0`` — nothing to
 *  compare against yet, not a spurious "increased from zero". */
export function computeMisfireRows(
  counts: Record<string, number>,
  baseline: Record<string, number>,
): MisfireCylinderRow[] {
  return Object.entries(counts)
    .map(([cylinder, count]) => ({
      cylinder,
      count,
      delta: count - (baseline[cylinder] ?? count),
    }))
    .sort((a, b) => Number(a.cylinder) - Number(b.cylinder));
}

/** On-board monitors (Task 3.1/3.5): per-cylinder misfire counts and raw
 *  Mode 06 monitor test results (value/min/max margins) from the daemon's
 *  periodic Mode 06 poll. Read-only, same convention as the DTC panel it
 *  sits beside in the diagnostics nav group. Cal-ID/CVN (Task 3.4) live in
 *  the Profiles panel; the Mode 02 freeze frame (Task 3.3) renders inside
 *  the DTC panel on the code it belongs to — neither is duplicated here. */
export function MonitorsPanel() {
  const fetcher = useCallback((signal: AbortSignal) => getMonitors(signal), []);
  const { data, error, loading } = useControlResource(fetcher, {
    intervalMs: 15000,
  });

  // Baseline misfire count per cylinder, captured from the first snapshot
  // this panel instance has seen — "delta since drive start" only means
  // something once there's a starting point to diff against. A page
  // refresh mid-drive re-baselines to whatever the ECU reports then; that's
  // the best this client-only state can do without a server-side session
  // marker, and still correctly highlights any *new* misfires since.
  // State set while rendering, not a ref: React re-renders at once with it,
  // so the first frame with data already diffs against itself.
  const [baseline, setBaseline] = useState<Record<string, number> | null>(null);
  if (data && baseline === null) {
    setBaseline({ ...data.misfire_counts });
  }

  if (loading) {
    return (
      <Panel title="Monitors">
        <p className={placeholderClass}>reading monitor sweep…</p>
      </Panel>
    );
  }
  if (error || !data) {
    return (
      <Panel title="Monitors">
        <p className={cn(placeholderClass, "text-bad")}>{error ?? "no data"}</p>
      </Panel>
    );
  }

  const cylinders = computeMisfireRows(data.misfire_counts, baseline ?? data.misfire_counts);
  const maxCount = Math.max(1, ...cylinders.map((c) => c.count));

  return (
    <Panel
      title="Monitors"
      count={`${data.results.length} test${data.results.length === 1 ? "" : "s"}`}
    >
      <p className="mb-3 text-[0.78rem] text-fg-dim">
        {data.captured_at ? `captured ${formatTimestamp(data.captured_at)}` : "never polled"}
        {data.stale && (
          <span className="ml-2 text-warn" role="status">
            ⚠ stale
          </span>
        )}
      </p>

      <h3 className="mb-1 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
        Misfire Counts
      </h3>
      {cylinders.length === 0 ? (
        <p className={cn(placeholderClass, "mb-3")}>none</p>
      ) : (
        <div className="mb-3 flex flex-col gap-1">
          {cylinders.map(({ cylinder, count, delta }) => (
            <div key={cylinder} className="flex items-center gap-2 text-[0.8rem]">
              <span className="w-14 shrink-0 font-num text-fg-dim">Cyl {cylinder}</span>
              <div className="h-3 flex-1 overflow-hidden rounded bg-border-soft">
                <div
                  className={cn("h-full", delta > 0 ? "bg-bad" : "bg-fg-dim/60")}
                  style={{ width: `${(count / maxCount) * 100}%` }}
                />
              </div>
              <span className="w-8 shrink-0 text-right font-num text-fg">{count}</span>
              {delta > 0 && (
                <span
                  className="w-10 shrink-0 text-right font-num text-bad"
                  title="since this page opened"
                >
                  +{delta}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      <h3 className="mb-1 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
        Monitor Test Results
      </h3>
      {data.results.length === 0 ? (
        <p className={placeholderClass}>none</p>
      ) : (
        <div className="flex flex-col gap-1">
          {data.results.map((result, index) => (
            <MonitorResultRow
              key={`${result.monitor_id}-${result.test_id}-${index}`}
              result={result}
            />
          ))}
        </div>
      )}
    </Panel>
  );
}

function MonitorResultRow({ result }: { result: MonitorTestResultEntry }) {
  const span = result.max_limit - result.min_limit;
  const fraction =
    span > 0 ? Math.min(1, Math.max(0, (result.test_value - result.min_limit) / span)) : 0.5;
  return (
    <div className="grid grid-cols-[6rem_1fr_auto] items-center gap-2 border-b border-border-soft pb-1 text-[0.78rem] last:border-0">
      <span className="font-num text-fg-dim">
        MID {result.monitor_id} / TID {result.test_id}
      </span>
      <div className="relative h-2 overflow-hidden rounded bg-border-soft">
        <div
          className={cn("absolute top-0 h-full w-[3px]", result.passed ? "bg-ok" : "bg-bad")}
          style={{ left: `${fraction * 100}%` }}
        />
      </div>
      <span className="w-28 shrink-0 text-right font-num text-fg-faint">
        {result.min_limit} / {result.test_value} / {result.max_limit}
      </span>
    </div>
  );
}

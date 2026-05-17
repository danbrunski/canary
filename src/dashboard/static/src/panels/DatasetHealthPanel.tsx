import { useCallback } from "react";

import { getDatasetHealth } from "../api/http";
import { Panel } from "../components/Panel";
import { useControlResource } from "../hooks/useControlResource";
import { cn, placeholderClass } from "../styles";
import type { DatasetSessionHealth } from "../types";

interface DatasetHealthPanelProps {
  token: string;
}

/** Dataset health (Task 6.23): the pre-retrain go/no-go. Corpus readiness
 *  verdict + reasons, per-session coverage, and channel availability so the
 *  operator knows what to capture next before burning Pi cycles. */
export function DatasetHealthPanel({ token }: DatasetHealthPanelProps) {
  const fetcher = useCallback(
    (signal: AbortSignal) => getDatasetHealth(token, signal),
    [token],
  );
  const { data, error, loading } = useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: 20000,
  });

  if (!token) {
    return (
      <Panel title="Dataset Health">
        <p className={placeholderClass}>
          Enter the control token (Control panel) to read dataset health.
        </p>
      </Panel>
    );
  }
  if (loading) {
    return (
      <Panel title="Dataset Health">
        <p className={placeholderClass}>analyzing corpus…</p>
      </Panel>
    );
  }
  if (error || !data) {
    return (
      <Panel title="Dataset Health">
        <p className={cn(placeholderClass, "text-bad")}>{error ?? "no data"}</p>
      </Panel>
    );
  }

  const ready = data.readiness.ready;
  const channels = Object.entries(data.channel_availability).sort(
    (a, b) => b[1] - a[1],
  );

  return (
    <Panel
      title="Dataset Health"
      count={`${data.sessions.length} sessions · ${data.total_rows} rows`}
    >
      <div
        className={cn(
          "mb-3 rounded-[8px] border px-3 py-2 text-[0.9rem] font-semibold",
          ready
            ? "border-ok/45 bg-ok/10 text-ok"
            : "border-warn/55 bg-warn/10 text-warn",
        )}
      >
        {ready ? "✓ Ready to retrain" : "✗ Not ready to retrain"}
        {!ready && (
          <ul className="mt-1.5 ml-4 list-disc text-[0.8rem] font-normal text-fg-dim">
            {data.readiness.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        )}
      </div>

      {Object.keys(data.mode_coverage).length > 0 && (
        <p className="mb-3 text-[0.8rem] text-fg-dim">
          modes:{" "}
          {Object.entries(data.mode_coverage)
            .map(([m, n]) => `${m} ${n}`)
            .join(" · ")}
        </p>
      )}

      <h3 className="mb-1 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
        Sessions
      </h3>
      <div className="mb-3 flex max-h-56 flex-col gap-1 overflow-y-auto">
        {data.sessions.map((s: DatasetSessionHealth) => (
          <div
            key={s.session_id}
            className="grid grid-cols-[1fr_auto] items-baseline gap-2 border-b border-border-soft pb-1 text-[0.82rem] last:border-0"
          >
            <span className="truncate">
              {s.pinned && <span title="pinned">📌 </span>}
              {s.training_candidate && <span className="text-accent">★ </span>}
              {s.session_id}
              {s.warnings.length > 0 && (
                <span className="ml-2 text-[0.74rem] text-warn">
                  ⚠ {s.warnings[0]}
                  {s.warnings.length > 1 && ` (+${s.warnings.length - 1})`}
                </span>
              )}
            </span>
            <span className="font-num tabular-nums text-fg-dim">
              {s.row_count} rows
            </span>
          </div>
        ))}
      </div>

      <h3 className="mb-1 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
        Channel availability
      </h3>
      <div className="flex max-h-40 flex-col gap-1 overflow-y-auto">
        {channels.map(([ch, frac]) => (
          <div key={ch} className="flex items-center gap-2 text-[0.78rem]">
            <span className="w-44 shrink-0 truncate text-fg-dim">{ch}</span>
            <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-elev-2">
              <span
                className="block h-full bg-accent-2"
                style={{ width: `${Math.round(frac * 100)}%` }}
              />
            </span>
            <span className="w-9 shrink-0 text-right font-num tabular-nums text-fg-faint">
              {Math.round(frac * 100)}%
            </span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

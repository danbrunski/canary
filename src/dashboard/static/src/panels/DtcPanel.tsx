import { useCallback } from "react";

import { getDtc } from "../api/http";
import { Panel } from "../components/Panel";
import { useControlResource } from "../hooks/useControlResource";
import { cn, placeholderClass } from "../styles";
import type { DtcCodeEntry } from "../types";
import { formatTimestamp } from "../util/time";

/** Trouble codes (Task 1.9): stored/pending/permanent DTCs from the daemon's
 *  periodic Mode 03/07/0A poll (Task 1.3), with MIL state and a staleness
 *  indicator. Read-only — there is no clear-codes control here or anywhere
 *  in the dashboard; that's an intentional design decision, not a gap. */
export function DtcPanel() {
  const fetcher = useCallback((signal: AbortSignal) => getDtc(signal), []);
  const { data, error, loading } = useControlResource(fetcher, {
    intervalMs: 15000,
  });

  if (loading) {
    return (
      <Panel title="Trouble Codes">
        <p className={placeholderClass}>reading DTC report…</p>
      </Panel>
    );
  }
  if (error || !data) {
    return (
      <Panel title="Trouble Codes">
        <p className={cn(placeholderClass, "text-bad")}>{error ?? "no data"}</p>
      </Panel>
    );
  }

  const milOn = data.mil_on;

  return (
    <Panel
      title="Trouble Codes"
      count={`${data.dtc_count} code${data.dtc_count === 1 ? "" : "s"}`}
    >
      <div
        className={cn(
          "mb-3 rounded-[8px] border px-3 py-2 text-[0.9rem] font-semibold",
          milOn
            ? "border-bad/55 bg-bad/10 text-bad"
            : "border-ok/45 bg-ok/10 text-ok",
        )}
      >
        {milOn == null ? "MIL state unknown" : milOn ? "MIL ON" : "MIL off"}
      </div>

      <p className="mb-3 text-[0.78rem] text-fg-dim">
        {data.captured_at ? `captured ${formatTimestamp(data.captured_at)}` : "never polled"}
        {data.stale && (
          <span className="ml-2 text-warn" role="status">
            ⚠ stale
          </span>
        )}
      </p>

      <CodeList title="Stored" codes={data.stored} />
      <CodeList title="Pending" codes={data.pending} />
      <CodeList title="Permanent" codes={data.permanent} />
    </Panel>
  );
}

function CodeList({ title, codes }: { title: string; codes: DtcCodeEntry[] }) {
  return (
    <div className="mb-3">
      <h3 className="mb-1 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
        {title} ({codes.length})
      </h3>
      {codes.length === 0 ? (
        <p className={placeholderClass}>none</p>
      ) : (
        <div className="flex flex-col gap-1">
          {codes.map((c) => (
            <div
              key={c.code}
              className="grid grid-cols-[5rem_1fr_auto] items-baseline gap-2 border-b border-border-soft pb-1 text-[0.82rem] last:border-0"
            >
              <span className="font-num font-bold text-fg">{c.code}</span>
              <span className="truncate text-fg-dim">{c.description}</span>
              <span className="text-[0.74rem] text-fg-faint">{c.system}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

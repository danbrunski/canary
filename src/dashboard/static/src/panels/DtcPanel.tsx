import { useCallback } from "react";

import { getDtc } from "../api/http";
import { Panel } from "../components/Panel";
import { useControlResource } from "../hooks/useControlResource";
import { cn, placeholderClass } from "../styles";
import type { DtcCodeEntry } from "../types";
import { formatTimestamp } from "../util/time";

/** Trouble codes (Task 1.9): stored/pending/permanent DTCs from the daemon's
 *  periodic Mode 03/07/0A poll (Task 1.3), with lamp state and a staleness
 *  indicator. Read-only — there is no clear-codes control here or anywhere
 *  in the dashboard; that's an intentional design decision, not a gap.
 *
 *  The lamp banner reads "MIL / CEL": MIL is the OBD-II spec name (and what
 *  the wire format, `mil_on`, and the `MIL_STATUS` channel are called), CEL
 *  is what it's called everywhere else. Both, so neither reader has to
 *  translate. */
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
        title="Malfunction Indicator Lamp — the check-engine light (Mode 01 PID 01, bit 7)"
        className={cn(
          "mb-3 rounded-[8px] border px-3 py-2 text-[0.9rem] font-semibold",
          milOn
            ? "border-bad/55 bg-bad/10 text-bad"
            : "border-ok/45 bg-ok/10 text-ok",
        )}
      >
        {milOn == null
          ? "MIL / CEL state unknown"
          : milOn
            ? "MIL / CEL ON"
            : "MIL / CEL off"}
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
      <FreezeFrame freezeFrame={data.freeze_frame} />
    </Panel>
  );
}

/** Task 3.3/3.5 — the Mode 02 snapshot read once when a stored code most
 *  recently first appeared this session, rendered inside the DTC panel
 *  (not a separate panel) since it belongs to the trouble-code report. */
function FreezeFrame({
  freezeFrame,
}: {
  freezeFrame: Record<string, number | string> | null;
}) {
  if (!freezeFrame || Object.keys(freezeFrame).length === 0) return null;
  return (
    <div>
      <h3 className="mb-1 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
        Freeze Frame
      </h3>
      <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[0.82rem]">
        {Object.entries(freezeFrame).map(([channel, value]) => (
          <div key={channel} className="flex justify-between gap-2">
            <span className="text-fg-dim">{channel}</span>
            <span className="font-num text-fg">{value}</span>
          </div>
        ))}
      </div>
    </div>
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

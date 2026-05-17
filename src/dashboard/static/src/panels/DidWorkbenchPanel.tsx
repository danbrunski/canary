import { useCallback } from "react";

import { getDids } from "../api/http";
import { Panel } from "../components/Panel";
import { useControlResource } from "../hooks/useControlResource";
import { cn, placeholderClass } from "../styles";
import type { DidEntry } from "../types";

const CONFIDENCE_STYLE: Record<string, string> = {
  confirmed: "text-ok",
  high: "text-accent",
  medium: "text-warn",
  low: "text-fg-dim",
  hypothesized: "text-fg-faint",
};

/** Nissan DID map / workbench (Task 6.16). Renders the read-only
 *  `data/dids.yaml` catalog — confidence, units, ranges, scaling, evidence
 *  notes. The parked-only live sweep + candidate-DID capture is gated on
 *  the 6.15 live DID profile (hardware), documented here so the constraint
 *  is explicit rather than implied. */
export function DidWorkbenchPanel() {
  const fetcher = useCallback((signal: AbortSignal) => getDids(signal), []);
  const { data, error, loading } = useControlResource(fetcher, {
    intervalMs: 30000,
  });

  if (loading) {
    return (
      <Panel title="Nissan DIDs">
        <p className={placeholderClass}>loading DID catalog…</p>
      </Panel>
    );
  }
  if (error || !data) {
    return (
      <Panel title="Nissan DIDs">
        <p className={cn(placeholderClass, "text-bad")}>{error ?? "no data"}</p>
      </Panel>
    );
  }

  return (
    <Panel title="Nissan DIDs" count={`${data.dids.length} known`}>
      <p className="mb-3 text-[0.78rem] text-fg-dim">
        Read-only Mode-22 catalog from <code>data/dids.yaml</code>. The
        parked-only live sweep + candidate capture is gated on the live DID
        profile (Task 6.15, hardware) — not exposed here yet.
      </p>
      {data.dids.length === 0 ? (
        <p className={placeholderClass}>
          no DIDs catalogued yet — run the RE sweep tooling and pin findings
          into <code>data/dids.yaml</code>.
        </p>
      ) : (
        <div className="max-h-[30rem] overflow-y-auto">
          <table className="w-full border-collapse text-[0.8rem]">
            <thead className="sticky top-0 bg-elev text-left text-[0.66rem] uppercase tracking-[0.1em] text-fg-faint">
              <tr>
                <th className="py-1 pr-2">DID</th>
                <th className="py-1 pr-2">Name</th>
                <th className="py-1 pr-2">Conf.</th>
                <th className="py-1 pr-2">Unit</th>
                <th className="py-1 pr-2">Scale</th>
                <th className="py-1">Range</th>
              </tr>
            </thead>
            <tbody className="font-num tabular-nums">
              {data.dids.map((d: DidEntry) => (
                <tr
                  key={d.did}
                  className="border-b border-border-soft align-top last:border-0"
                >
                  <td className="py-1 pr-2 text-fg-dim">
                    0x{d.did.toString(16).toUpperCase().padStart(4, "0")}
                  </td>
                  <td className="py-1 pr-2">
                    {d.name}
                    {d.notes && (
                      <span className="block text-[0.72rem] font-normal text-fg-faint">
                        {d.notes}
                      </span>
                    )}
                  </td>
                  <td
                    className={cn(
                      "py-1 pr-2",
                      CONFIDENCE_STYLE[d.confidence] ?? "text-fg-dim",
                    )}
                  >
                    {d.confidence}
                  </td>
                  <td className="py-1 pr-2 text-fg-dim">{d.unit || "—"}</td>
                  <td className="py-1 pr-2 text-fg-dim">
                    {d.scale != null
                      ? `×${d.scale}${d.offset ? ` ${d.offset >= 0 ? "+" : ""}${d.offset}` : ""}`
                      : "raw"}
                  </td>
                  <td className="py-1 text-fg-dim">
                    {d.plausible_min != null || d.plausible_max != null
                      ? `${d.plausible_min ?? "?"}…${d.plausible_max ?? "?"}`
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

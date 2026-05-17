import { useCallback, useState } from "react";

import { getObdCoverage } from "../api/http";
import { Panel } from "../components/Panel";
import { useControlResource } from "../hooks/useControlResource";
import { buttonClass, cn, placeholderClass } from "../styles";
import type { ObdCoverageRow, PidCoverageStatus } from "../types";

const STATUS_STYLE: Record<PidCoverageStatus, string> = {
  decoded: "text-ok",
  supported_not_decoded: "text-warn",
  unsupported: "text-fg-faint",
  unknown_pid: "text-fg-dim",
};

const STATUS_LABEL: Record<PidCoverageStatus, string> = {
  decoded: "decoded",
  supported_not_decoded: "supported · undecoded",
  unsupported: "unsupported",
  unknown_pid: "unknown",
};

/** OBD coverage (Task 6.3): the honest proof behind the channel count —
 *  which public PIDs the ECU supports, which CANary decodes, and the
 *  supported-but-undecoded gap (Risk R7), inferred from observed telemetry.
 *  Read-only + unauthenticated, like `/status`. */
export function ObdCoveragePanel() {
  const fetcher = useCallback((signal: AbortSignal) => getObdCoverage(signal), []);
  const { data, error, loading } = useControlResource(fetcher, {
    intervalMs: 15000,
  });
  const [showAll, setShowAll] = useState(false);

  if (loading) {
    return (
      <Panel title="OBD Coverage">
        <p className={placeholderClass}>probing…</p>
      </Panel>
    );
  }
  if (error || !data) {
    return (
      <Panel title="OBD Coverage">
        <p className={cn(placeholderClass, "text-bad")}>
          {error ?? "no coverage data"}
        </p>
      </Panel>
    );
  }

  const rows = showAll
    ? data.rows
    : data.rows.filter((r) => r.status !== "unsupported");

  return (
    <Panel
      title="OBD Coverage"
      count={`${data.decoded_count} decoded / ${data.supported_count} supported`}
    >
      <div className="mb-3 flex flex-wrap gap-2 text-[0.78rem]">
        <Stat label="supported" value={data.supported_count} />
        <Stat label="decoded" value={data.decoded_count} tone="text-ok" />
        <Stat
          label="undecoded gap"
          value={data.supported_not_decoded_count}
          tone={data.supported_not_decoded_count > 0 ? "text-warn" : undefined}
        />
        <Stat label="responding" value={data.responded_count} />
        <Stat
          label="backed off"
          value={data.backed_off_count}
          tone={data.backed_off_count > 0 ? "text-bad" : undefined}
        />
      </div>

      {data.undecoded_supported.length > 0 && (
        <p className="mb-3 text-[0.8rem] text-fg-dim">
          <span className="text-warn">supported but not decoded:</span>{" "}
          {data.undecoded_supported.join(", ")}
        </p>
      )}

      <div className="mb-2 flex items-center justify-between">
        <span className="text-[0.74rem] text-fg-faint">
          {rows.length} {showAll ? "PIDs" : "supported/known PIDs"}
        </span>
        <button
          type="button"
          className={cn(buttonClass, "px-2.5 py-1 text-[0.74rem]")}
          onClick={() => setShowAll((v) => !v)}
        >
          {showAll ? "hide unsupported" : "show all"}
        </button>
      </div>

      <div className="max-h-[28rem] overflow-y-auto">
        <table className="w-full border-collapse text-[0.8rem]">
          <thead className="sticky top-0 bg-elev text-left text-[0.66rem] uppercase tracking-[0.1em] text-fg-faint">
            <tr>
              <th className="py-1 pr-2">PID</th>
              <th className="py-1 pr-2">Channel</th>
              <th className="py-1 pr-2">Status</th>
              <th className="py-1 pr-2">Tier</th>
              <th className="py-1">Live</th>
            </tr>
          </thead>
          <tbody className="font-num tabular-nums">
            {rows.map((r: ObdCoverageRow) => (
              <tr key={r.pid} className="border-b border-border-soft last:border-0">
                <td className="py-1 pr-2 text-fg-dim">{r.pid_hex}</td>
                <td className="py-1 pr-2">
                  {r.display_name ?? r.channel ?? "—"}
                </td>
                <td className={cn("py-1 pr-2", STATUS_STYLE[r.status])}>
                  {STATUS_LABEL[r.status]}
                </td>
                <td className="py-1 pr-2 text-fg-dim">{r.poll_tier ?? "—"}</td>
                <td className="py-1">
                  {r.backed_off ? (
                    <span className="text-bad">backoff</span>
                  ) : r.responded_recently ? (
                    <span className="text-ok">●</span>
                  ) : (
                    <span className="text-fg-faint">·</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: string;
}) {
  return (
    <span className="inline-flex items-baseline gap-1 rounded-full border border-border-soft bg-black/25 px-2.5 py-1">
      <b className={cn("font-num tabular-nums", tone ?? "text-fg")}>{value}</b>
      <span className="text-[0.62rem] uppercase tracking-[0.12em] text-fg-faint">
        {label}
      </span>
    </span>
  );
}

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import {
  cn,
  definitionListClass,
  definitionRowClass,
  definitionTermClass,
  definitionValueClass,
  panelClass,
  panelHeaderClass,
  panelTitleClass,
  placeholderClass,
} from "../styles";
import type { StatusResponse } from "../types";

interface SystemPanelProps {
  status: StatusResponse | null;
  channels: Map<string, ChannelSnapshot>;
  statusError: Error | null;
}

/** System health: storage, latest session, vehicle line, data freshness.
 *
 *  Stale-data detection uses the newest timestamp seen across all
 *  channels — if it's more than 10 s old, surface "stale data" so the
 *  operator knows the daemon stopped emitting (vs. just "no anomalies"). */
export function SystemPanel({ status, channels, statusError }: SystemPanelProps) {
  const freshness = computeFreshness(channels);
  const stale = freshness !== null && freshness > 10_000;

  return (
    <section className={panelClass}>
      <header className={panelHeaderClass}>
        <h2 className={panelTitleClass}>System</h2>
      </header>
      {statusError && (
        <div className="mb-2 rounded-md bg-bad/12 px-2 py-1.5 text-[0.82rem] text-bad">
          /status unreachable: {statusError.message}
        </div>
      )}
      {!status ? (
        <p className={placeholderClass}>loading…</p>
      ) : (
        <dl className={definitionListClass}>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>vehicle</dt>
            <dd className={definitionValueClass}>{status.vehicle || "unknown"}</dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>storage</dt>
            <dd className={definitionValueClass}>
              {status.free_mb.toFixed(0)} MB free{" "}
              <span className="text-fg-dim">at {status.storage_path}</span>
            </dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>latest session</dt>
            <dd className={definitionValueClass}>{status.latest_session ?? "none yet"}</dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>data freshness</dt>
            <dd className={cn(definitionValueClass, stale && "text-bad")}>
              {freshness === null ? "—" : `${(freshness / 1000).toFixed(1)}s`}
              {stale && " (stale)"}
            </dd>
          </div>
        </dl>
      )}
    </section>
  );
}

function computeFreshness(channels: Map<string, ChannelSnapshot>): number | null {
  let newest: number | null = null;
  for (const snap of channels.values()) {
    const ts = snap.latest.timestamp;
    if (!ts) continue;
    const ms = Date.parse(ts);
    if (Number.isFinite(ms) && (newest === null || ms > newest)) {
      newest = ms;
    }
  }
  if (newest === null) return null;
  return Date.now() - newest;
}

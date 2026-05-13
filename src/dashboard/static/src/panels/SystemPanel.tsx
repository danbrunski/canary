import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
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
    <section className="panel system-panel">
      <header>
        <h2>System</h2>
      </header>
      {statusError && (
        <div className="error">
          /status unreachable: {statusError.message}
        </div>
      )}
      {!status ? (
        <p className="placeholder">loading…</p>
      ) : (
        <dl>
          <div>
            <dt>vehicle</dt>
            <dd>{status.vehicle || "unknown"}</dd>
          </div>
          <div>
            <dt>storage</dt>
            <dd>
              {status.free_mb.toFixed(0)} MB free <span className="dim">at {status.storage_path}</span>
            </dd>
          </div>
          <div>
            <dt>latest session</dt>
            <dd>{status.latest_session ?? "none yet"}</dd>
          </div>
          <div>
            <dt>data freshness</dt>
            <dd className={stale ? "stale" : ""}>
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

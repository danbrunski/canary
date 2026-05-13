import { useMemo } from "react";

import { Sparkline } from "../components/Sparkline";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";

interface TelemetryPanelProps {
  channels: Map<string, ChannelSnapshot>;
}

/** Channels the driving view should foreground (Task 5.9). The dashboard
 *  shows these big and at the top; everything else lands in a "more" grid.
 *  Order matters — left-to-right in the layout. */
const PRIMARY_CHANNELS = [
  "obd:RPM",
  "obd:SPEED",
  "obd:COOLANT_TEMP",
  "obd:MAF",
  "obd:THROTTLE_POS",
  "obd:CONTROL_MODULE_VOLTAGE",
];

/** Render current values + 2-minute sparklines for every observed channel.
 *  Channels we don't have an entry for under PRIMARY_CHANNELS still render
 *  in the secondary grid — the daemon emits whatever the producer hands it,
 *  and we want to see *everything* that's flowing, not just a curated set. */
export function TelemetryPanel({ channels }: TelemetryPanelProps) {
  const { primary, secondary } = useMemo(() => {
    const primary: [string, ChannelSnapshot][] = [];
    const secondary: [string, ChannelSnapshot][] = [];
    // Primary list in fixed order
    for (const key of PRIMARY_CHANNELS) {
      const snap = channels.get(key);
      if (snap) primary.push([key, snap]);
    }
    // Secondary: everything else, alphabetical by key
    const primarySet = new Set(PRIMARY_CHANNELS);
    for (const [key, snap] of [...channels].sort(([a], [b]) => a.localeCompare(b))) {
      if (!primarySet.has(key)) secondary.push([key, snap]);
    }
    return { primary, secondary };
  }, [channels]);

  if (channels.size === 0) {
    return (
      <section className="panel telemetry-panel">
        <header>
          <h2>Telemetry</h2>
        </header>
        <p className="placeholder">no data yet — waiting for the collectors to produce.</p>
      </section>
    );
  }

  return (
    <section className="panel telemetry-panel">
      <header>
        <h2>Telemetry</h2>
        <span className="count">{channels.size} channels</span>
      </header>
      <div className="primary-grid">
        {primary.map(([key, snap]) => (
          <ChannelCard key={key} channelKey={key} snapshot={snap} large />
        ))}
      </div>
      {secondary.length > 0 && (
        <details className="more-channels">
          <summary>{secondary.length} more channels</summary>
          <div className="secondary-grid">
            {secondary.map(([key, snap]) => (
              <ChannelCard key={key} channelKey={key} snapshot={snap} />
            ))}
          </div>
        </details>
      )}
    </section>
  );
}

function ChannelCard({
  channelKey,
  snapshot,
  large = false,
}: {
  channelKey: string;
  snapshot: ChannelSnapshot;
  large?: boolean;
}) {
  const value = formatValue(snapshot.latest.value);
  const unit = snapshot.latest.unit ?? "";
  return (
    <div className={large ? "channel-card large" : "channel-card"}>
      <div className="channel-key">{prettyChannel(channelKey)}</div>
      <div className="channel-value">
        {value}
        {unit && <span className="channel-unit">{unit}</span>}
      </div>
      {snapshot.history.length > 1 && (
        <Sparkline
          values={snapshot.history}
          width={large ? 200 : 120}
          height={large ? 44 : 28}
        />
      )}
    </div>
  );
}

/** Drop the source prefix so the card reads `RPM` instead of `obd:RPM`. */
function prettyChannel(key: string): string {
  const idx = key.indexOf(":");
  if (idx === -1) return key;
  return key.slice(idx + 1);
}

function formatValue(value: number | string | boolean | null): string {
  if (value === null) return "—";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return "—";
    return Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(2);
  }
  return String(value);
}

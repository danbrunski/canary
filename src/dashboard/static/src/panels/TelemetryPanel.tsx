import { useMemo } from "react";

import { Sparkline } from "../components/Sparkline";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { BindingDisplay, BindingsByChannel } from "../types";

interface TelemetryPanelProps {
  channels: Map<string, ChannelSnapshot>;
  bindings: BindingsByChannel;
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

/** Render current values + 2-minute sparklines. Decoded CAN channels are
 *  rendered in engineering units when their binding has a pinned scale —
 *  ``decoded:ECM_COOLANT_TEMP`` raw=105 becomes "65 °C". When the binding
 *  has plausibility bounds, values outside them are color-flagged so an
 *  operator sees the abnormal reading immediately. */
export function TelemetryPanel({ channels, bindings }: TelemetryPanelProps) {
  const { primary, secondary } = useMemo(() => {
    const primary: [string, ChannelSnapshot][] = [];
    const secondary: [string, ChannelSnapshot][] = [];
    for (const key of PRIMARY_CHANNELS) {
      const snap = channels.get(key);
      if (snap) primary.push([key, snap]);
    }
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
          <ChannelCard
            key={key}
            channelKey={key}
            snapshot={snap}
            binding={bindings[key]}
            large
          />
        ))}
      </div>
      {secondary.length > 0 && (
        <details className="more-channels">
          <summary>{secondary.length} more channels</summary>
          <div className="secondary-grid">
            {secondary.map(([key, snap]) => (
              <ChannelCard
                key={key}
                channelKey={key}
                snapshot={snap}
                binding={bindings[key]}
              />
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
  binding,
  large = false,
}: {
  channelKey: string;
  snapshot: ChannelSnapshot;
  binding?: BindingDisplay;
  large?: boolean;
}) {
  const rendered = renderChannel(snapshot, binding);
  const sparkValues = sparkSeries(snapshot, binding);
  return (
    <div className={`channel-card ${large ? "large" : ""} ${rendered.bandClass}`}>
      <div className="channel-key">{prettyChannel(channelKey)}</div>
      <div className="channel-value">
        {rendered.text}
        {rendered.unit && <span className="channel-unit">{rendered.unit}</span>}
      </div>
      {sparkValues.length > 1 && (
        <Sparkline
          values={sparkValues}
          width={large ? 200 : 120}
          height={large ? 44 : 28}
        />
      )}
    </div>
  );
}

interface RenderedValue {
  text: string;
  unit: string;
  bandClass: "" | "band-warn" | "band-bad";
}

/** Apply binding scale/offset/unit, decide whether to color the card. */
export function renderChannel(
  snapshot: ChannelSnapshot,
  binding: BindingDisplay | undefined,
): RenderedValue {
  const raw = snapshot.latest.value;
  if (raw === null || raw === undefined) {
    return { text: "—", unit: "", bandClass: "" };
  }
  // Non-numeric (VIN string, bool) — pass through, no binding math applies
  if (typeof raw === "string" || typeof raw === "boolean") {
    return { text: String(raw), unit: snapshot.latest.unit ?? "", bandClass: "" };
  }
  if (!Number.isFinite(raw)) {
    return { text: "—", unit: "", bandClass: "" };
  }
  // Apply binding scale + offset when present. Decoded CAN channels carry
  // the raw integer from the bytes; OBD channels are pre-scaled by
  // python-obd so the binding doesn't apply to them, but the unit + bounds
  // still do.
  const isDecoded = snapshot.latest.source === "decoded";
  let value = raw;
  if (binding && binding.scale !== null && isDecoded) {
    value = raw * binding.scale + (binding.offset ?? 0);
  }
  const unit = binding?.unit ?? snapshot.latest.unit ?? "";
  const text = formatNumber(value);

  // Plausibility-band coloring. Warning when value is on the boundary
  // (>= 90% of the range below min or above max). Bad when outside.
  let bandClass: "" | "band-warn" | "band-bad" = "";
  if (binding) {
    const lo = binding.plausible_min;
    const hi = binding.plausible_max;
    if ((lo !== null && value < lo) || (hi !== null && value > hi)) {
      bandClass = "band-bad";
    } else if (lo !== null && hi !== null) {
      const span = hi - lo;
      const margin = span * 0.05;
      if (value < lo + margin || value > hi - margin) bandClass = "band-warn";
    }
  }

  return { text, unit, bandClass };
}

/** Compute the sparkline series in engineering units when scale is pinned —
 *  otherwise the chart's auto-axis would interpret raw bytes, which looks
 *  weird (a coolant chart bottoming at 53 isn't useful). */
function sparkSeries(
  snapshot: ChannelSnapshot,
  binding: BindingDisplay | undefined,
): number[] {
  if (
    !binding ||
    binding.scale === null ||
    snapshot.latest.source !== "decoded"
  ) {
    return snapshot.history;
  }
  const offset = binding.offset ?? 0;
  return snapshot.history.map((raw) => raw * binding.scale! + offset);
}

/** Drop the source prefix so the card reads `RPM` instead of `obd:RPM`. */
function prettyChannel(key: string): string {
  const idx = key.indexOf(":");
  if (idx === -1) return key;
  return key.slice(idx + 1);
}

function formatNumber(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 100) return value.toFixed(0);
  if (abs >= 10) return value.toFixed(1);
  return value.toFixed(2);
}

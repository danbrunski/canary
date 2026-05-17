import { useEffect, useMemo, useState } from "react";

import { Sparkline } from "../components/Sparkline";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { BindingDisplay, BindingsByChannel } from "../types";

interface TelemetryPanelProps {
  channels: Map<string, ChannelSnapshot>;
  bindings: BindingsByChannel;
  compact?: boolean;
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
export function TelemetryPanel({ channels, bindings, compact = false }: TelemetryPanelProps) {
  const [query, setQuery] = useState("");
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());

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

  const visibleChannels = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return [...channels]
      .sort(([a], [b]) => a.localeCompare(b))
      .filter(([key, snap]) => {
        if (!needle) return true;
        return (
          key.toLowerCase().includes(needle) ||
          String(snap.latest.value ?? "").toLowerCase().includes(needle) ||
          (snap.latest.unit ?? "").toLowerCase().includes(needle)
        );
      });
  }, [channels, query]);

  useEffect(() => {
    if (selectedKeys.size > 0 || channels.size === 0) return;
    const defaults = PRIMARY_CHANNELS.filter((key) => channels.has(key)).slice(0, 4);
    setSelectedKeys(new Set(defaults.length > 0 ? defaults : [...channels.keys()].slice(0, 4)));
  }, [channels, selectedKeys.size]);

  const chartKeys = useMemo(
    () => [...selectedKeys].filter((key) => channels.has(key)).slice(0, 6),
    [channels, selectedKeys],
  );

  const toggleSelected = (key: string) => {
    setSelectedKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        if (next.size >= 6) {
          next.delete([...next][0]);
        }
        next.add(key);
      }
      return next;
    });
  };

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
    <section className={`panel telemetry-panel ${compact ? "compact" : ""}`}>
      <header>
        <div>
          <h2>Telemetry</h2>
          <span className="count">{channels.size} channels</span>
        </div>
        <input
          className="channel-search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="search PID or signal"
        />
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
      <TelemetryChart
        channels={channels}
        bindings={bindings}
        selectedKeys={chartKeys}
      />
      {!compact && (
        <div className="channel-browser">
          <div className="browser-header">
            <b>Channel explorer</b>
            <span>{visibleChannels.length} visible / select up to 6 to chart</span>
          </div>
          <div className="channel-table">
            {visibleChannels.map(([key, snap]) => (
              <ChannelRow
                key={key}
                channelKey={key}
                snapshot={snap}
                binding={bindings[key]}
                selected={selectedKeys.has(key)}
                onToggle={() => toggleSelected(key)}
              />
            ))}
          </div>
        </div>
      )}
      {compact && secondary.length > 0 && (
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

function ChannelRow({
  channelKey,
  snapshot,
  binding,
  selected,
  onToggle,
}: {
  channelKey: string;
  snapshot: ChannelSnapshot;
  binding?: BindingDisplay;
  selected: boolean;
  onToggle: () => void;
}) {
  const rendered = renderChannel(snapshot, binding);
  return (
    <button
      type="button"
      className={`channel-row ${selected ? "selected" : ""} ${rendered.bandClass}`}
      onClick={onToggle}
    >
      <span className="row-select">{selected ? "on" : "off"}</span>
      <span className="row-channel">
        <b>{prettyChannel(channelKey)}</b>
        <small>{snapshot.latest.source}</small>
      </span>
      <span className="row-value">
        {rendered.text}
        {rendered.unit && <em>{rendered.unit}</em>}
      </span>
      <span className="row-age">{formatAge(snapshot.latest.timestamp)}</span>
    </button>
  );
}

function TelemetryChart({
  channels,
  bindings,
  selectedKeys,
}: {
  channels: Map<string, ChannelSnapshot>;
  bindings: BindingsByChannel;
  selectedKeys: string[];
}) {
  const series = selectedKeys
    .map((key) => {
      const snapshot = channels.get(key);
      if (!snapshot) return null;
      return {
        key,
        label: prettyChannel(key),
        values: sparkSeries(snapshot, bindings[key]).filter(Number.isFinite),
      };
    })
    .filter((item): item is { key: string; label: string; values: number[] } => {
      return item !== null && item.values.length > 1;
    });

  if (series.length === 0) {
    return (
      <div className="telemetry-chart empty">
        Select numeric channels to chart live history.
      </div>
    );
  }

  const allValues = series.flatMap((item) => item.values);
  const min = Math.min(...allValues);
  const max = Math.max(...allValues);
  const span = max === min ? 1 : max - min;
  const width = 900;
  const height = 220;
  const pad = 22;
  const colors = ["#7cffcb", "#ffb84a", "#7cc7ff", "#ff6b6b", "#c9a7ff", "#ffd166"];

  const pathFor = (values: number[]) =>
    values
      .map((value, index) => {
        const x = pad + (index / Math.max(values.length - 1, 1)) * (width - pad * 2);
        const y = height - pad - ((value - min) / span) * (height - pad * 2);
        return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");

  return (
    <div className="telemetry-chart">
      <div className="chart-meta">
        <span>{formatNumber(min)}</span>
        <b>Live chart</b>
        <span>{formatNumber(max)}</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="telemetry chart">
        <line x1={pad} y1={height - pad} x2={width - pad} y2={height - pad} />
        <line x1={pad} y1={pad} x2={pad} y2={height - pad} />
        {series.map((item, index) => (
          <path
            key={item.key}
            d={pathFor(item.values)}
            stroke={colors[index % colors.length]}
          />
        ))}
      </svg>
      <div className="chart-legend">
        {series.map((item, index) => (
          <span key={item.key} style={{ color: colors[index % colors.length] }}>
            {item.label}
          </span>
        ))}
      </div>
    </div>
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

function formatAge(timestamp: string | null): string {
  if (!timestamp) return "-";
  const ageMs = Date.now() - Date.parse(timestamp);
  if (!Number.isFinite(ageMs)) return "-";
  if (ageMs < 1000) return "now";
  return `${Math.round(ageMs / 1000)}s`;
}

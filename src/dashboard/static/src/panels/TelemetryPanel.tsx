import { useEffect, useMemo, useState } from "react";

import { Sparkline } from "../components/Sparkline";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import {
  buttonClass,
  cn,
  inputClass,
  panelClass,
  panelCountClass,
  panelHeaderCenterClass,
  panelTitleClass,
  placeholderClass,
} from "../styles";
import type { BindingDisplay, BindingsByChannel } from "../types";

interface TelemetryPanelProps {
  channels: Map<string, ChannelSnapshot>;
  bindings: BindingsByChannel;
  /** Drive view: equal-prominence tile grid + a user-pinned subset, no
   *  hero tile, nothing hidden behind an expander. */
  compact?: boolean;
}

/** Sensible default pins for a standard OBD capture (Drive view). Only used
 *  to seed the pin set the first time, and only the ones that actually
 *  exist — a decoded-CAN replay has none of these, so it defaults to
 *  "show everything, equally". */
const DEFAULT_PIN_CANDIDATES = [
  "obd:RPM",
  "obd:SPEED",
  "obd:COOLANT_TEMP",
  "obd:MAF",
  "obd:THROTTLE_POS",
  "obd:CONTROL_MODULE_VOLTAGE",
];

const PIN_STORAGE_KEY = "canary.drive.pinned";

function loadPins(): string[] {
  try {
    const raw = window.localStorage.getItem(PIN_STORAGE_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

/** Render current values + 2-minute sparklines. Decoded CAN channels are
 *  rendered in engineering units when their binding has a pinned scale —
 *  ``decoded:ECM_RPM`` raw=16376 becomes "2047 rpm". When the binding has
 *  plausibility bounds, values outside them are color-flagged. */
export function TelemetryPanel({ channels, bindings, compact = false }: TelemetryPanelProps) {
  const [query, setQuery] = useState("");
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [pinned, setPinned] = useState<string[]>(loadPins);
  const [pinnedOnly, setPinnedOnly] = useState(false);

  useEffect(() => {
    try {
      window.localStorage.setItem(PIN_STORAGE_KEY, JSON.stringify(pinned));
    } catch {
      /* storage disabled — pins still live for the session */
    }
  }, [pinned]);

  const displayChannels = useMemo(() => {
    return [...channels]
      .filter(([_key, snap]) => !isEmptyRawCan(snap))
      .sort(([a], [b]) => a.localeCompare(b));
  }, [channels]);

  // Seed default pins once (only with channels that exist). A decoded
  // replay has none of the OBD defaults → stays empty → equal grid of all.
  useEffect(() => {
    if (pinned.length > 0 || displayChannels.length === 0) return;
    const seed = DEFAULT_PIN_CANDIDATES.filter((k) => channels.has(k));
    if (seed.length > 0) setPinned(seed);
  }, [channels, displayChannels.length, pinned.length]);

  const pinnedSet = useMemo(() => new Set(pinned), [pinned]);

  const togglePin = (key: string) =>
    setPinned((cur) =>
      cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key],
    );

  const visibleChannels = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return displayChannels.filter(([key, snap]) => {
      if (!needle) return true;
      return (
        key.toLowerCase().includes(needle) ||
        String(snap.latest.value ?? "").toLowerCase().includes(needle) ||
        (snap.latest.unit ?? "").toLowerCase().includes(needle)
      );
    });
  }, [displayChannels, query]);

  // Drive grid order: pinned first (in pin order), then the rest, all the
  // same size. "Pinned only" collapses to just the curated set.
  const gridChannels = useMemo(() => {
    const byKey = new Map(visibleChannels);
    const pins = pinned
      .filter((k) => byKey.has(k))
      .map((k) => [k, byKey.get(k)!] as [string, ChannelSnapshot]);
    if (pinnedOnly && pins.length > 0) return pins;
    const rest = visibleChannels.filter(([k]) => !pinnedSet.has(k));
    return [...pins, ...rest];
  }, [visibleChannels, pinned, pinnedSet, pinnedOnly]);

  useEffect(() => {
    if (selectedKeys.size > 0 || displayChannels.length === 0) return;
    const numericFallback = displayChannels
      .filter(([_key, snap]) => snap.history.length > 0)
      .map(([key]) => key)
      .slice(0, 4);
    setSelectedKeys(new Set(numericFallback));
  }, [displayChannels, selectedKeys.size]);

  // The chart follows the pins in Drive (curating tiles curates the chart);
  // in the explorer it follows the row selection.
  const chartKeys = useMemo(() => {
    if (compact) {
      const fromPins = pinned.filter(
        (k) => (channels.get(k)?.history.length ?? 0) > 1,
      );
      if (fromPins.length > 0) return fromPins.slice(0, 6);
      return displayChannels
        .filter(([, s]) => s.history.length > 1)
        .map(([k]) => k)
        .slice(0, 6);
    }
    return [...selectedKeys].filter((key) => channels.has(key)).slice(0, 6);
  }, [compact, pinned, channels, displayChannels, selectedKeys]);

  const toggleSelected = (key: string) => {
    setSelectedKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        if (next.size >= 6) next.delete([...next][0]);
        next.add(key);
      }
      return next;
    });
  };

  if (channels.size === 0) {
    return (
      <section className={cn(panelClass, compact && "col-span-2 row-start-1 max-[900px]:col-span-1")}>
        <header className={panelHeaderCenterClass}>
          <h2 className={panelTitleClass}>Telemetry</h2>
        </header>
        <p className={placeholderClass}>no data yet — waiting for the collectors to produce.</p>
      </section>
    );
  }

  return (
    <section className={cn(panelClass, compact && "col-span-2 row-start-1 max-[900px]:col-span-1")}>
      <header className={panelHeaderCenterClass}>
        <div>
          <h2 className={panelTitleClass}>Telemetry</h2>
          <span className={panelCountClass}>
            {displayChannels.length} live values
            {compact && pinned.length > 0 && ` · ${pinned.length} pinned`}
          </span>
        </div>
        <div className="flex items-center gap-2">
          {compact && pinned.length > 0 && (
            <button
              type="button"
              className={cn(
                buttonClass,
                "px-2.5 py-1 text-[0.74rem]",
                pinnedOnly && "border-accent text-accent",
              )}
              onClick={() => setPinnedOnly((v) => !v)}
              title="Show only the channels you've pinned"
            >
              {pinnedOnly ? "★ pinned only" : "☆ pinned only"}
            </button>
          )}
          <input
            className={cn(inputClass, "w-[min(20rem,40vw)] max-[700px]:w-full")}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="search PID or signal"
          />
        </div>
      </header>

      {compact ? (
        <>
          {gridChannels.length === 0 ? (
            <p className={placeholderClass}>no channels match “{query}”.</p>
          ) : (
            <div className="grid grid-cols-[repeat(auto-fill,minmax(165px,1fr))] gap-2.5">
              {gridChannels.map(([key, snap]) => (
                <ChannelCard
                  key={key}
                  channelKey={key}
                  snapshot={snap}
                  binding={bindings[key]}
                  pinned={pinnedSet.has(key)}
                  onTogglePin={() => togglePin(key)}
                />
              ))}
            </div>
          )}
          <TelemetryChart channels={channels} bindings={bindings} selectedKeys={chartKeys} />
          {pinned.length === 0 && displayChannels.length > 6 && (
            <p className="mt-2 text-[0.74rem] text-fg-faint">
              Tip: ☆ a channel to pin it to the top — then “pinned only” for a
              focused drive view.
            </p>
          )}
        </>
      ) : (
        <>
          <TelemetryChart channels={channels} bindings={bindings} selectedKeys={chartKeys} />
          <div className="mt-3.5">
            <div className="mb-1.5 flex justify-between gap-2 text-[0.78rem] text-fg-dim">
              <b className="text-fg">Channel explorer</b>
              <span>{visibleChannels.length} visible / select up to 6 to chart</span>
            </div>
            <div className="grid max-h-[44rem] grid-cols-[repeat(auto-fit,minmax(270px,1fr))] gap-1.5 overflow-y-auto">
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
        </>
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
      className={cn(
        "grid w-full grid-cols-[2.3rem_minmax(0,1fr)_auto_3rem] items-center gap-2 rounded-tile border border-border-soft bg-[linear-gradient(180deg,var(--color-elev-2),var(--color-elev))] px-2.5 py-2 text-left text-fg transition-[border-color,transform] duration-150 hover:-translate-y-px hover:border-border",
        selected && "border-accent ring-1 ring-inset ring-accent/45",
        bandUtility(rendered.bandClass),
      )}
      onClick={onToggle}
    >
      <span className={cn("text-[0.68rem] uppercase text-fg-dim", selected && "text-accent")}>
        {selected ? "on" : "off"}
      </span>
      <span className="flex min-w-0 flex-col">
        <b className="overflow-hidden text-ellipsis whitespace-nowrap">{prettyChannel(channelKey)}</b>
        <small className="text-[0.72rem] text-fg-dim">{snapshot.latest.source}</small>
      </span>
      <span className="font-num font-bold tabular-nums">
        {rendered.text}
        {rendered.unit && (
          <em className="ml-1 text-[0.72rem] not-italic text-fg-dim">{rendered.unit}</em>
        )}
      </span>
      <span className="text-[0.72rem] text-fg-dim">{formatAge(snapshot.latest.timestamp)}</span>
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
      <div className="mt-3.5 flex min-h-16 items-center rounded-[8px] border border-border bg-bg p-2.5 text-[0.85rem] text-fg-dim">
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
    <div className="mt-3.5 rounded-[8px] border border-border bg-bg p-2.5">
      <div className="flex justify-between gap-3 font-num text-[0.78rem] tabular-nums text-fg-dim">
        <span>{formatNumber(min)}</span>
        <b>Live chart</b>
        <span>{formatNumber(max)}</span>
      </div>
      <svg
        className="block h-[clamp(12rem,34vh,22rem)] w-full"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label="telemetry chart"
      >
        <line
          className="stroke-border-soft [stroke-width:2]"
          x1={pad}
          y1={height - pad}
          x2={width - pad}
          y2={height - pad}
        />
        <line
          className="stroke-border-soft [stroke-width:2]"
          x1={pad}
          y1={pad}
          x2={pad}
          y2={height - pad}
        />
        {series.map((item, index) => (
          <path
            key={item.key}
            className="fill-none [stroke-linecap:round] [stroke-linejoin:round] [stroke-width:3]"
            d={pathFor(item.values)}
            stroke={colors[index % colors.length]}
          />
        ))}
      </svg>
      <div className="flex flex-wrap justify-start gap-3 font-num text-[0.78rem] tabular-nums text-fg-dim">
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
  pinned,
  onTogglePin,
}: {
  channelKey: string;
  snapshot: ChannelSnapshot;
  binding?: BindingDisplay;
  pinned?: boolean;
  onTogglePin?: () => void;
}) {
  const rendered = renderChannel(snapshot, binding);
  const sparkValues = sparkSeries(snapshot, binding);
  return (
    <div
      className={cn(
        "relative rounded-tile border border-border-soft bg-[linear-gradient(180deg,var(--color-elev-2),var(--color-elev))] px-3 py-2 transition-[border-color,box-shadow,transform] duration-150 hover:-translate-y-px hover:border-border hover:shadow-panel",
        pinned && "border-accent/55",
        bandUtility(rendered.bandClass),
      )}
    >
      {onTogglePin && (
        <button
          type="button"
          className={cn(
            "absolute right-1.5 top-1.5 text-[0.8rem] leading-none transition-colors",
            pinned ? "text-accent" : "text-fg-faint hover:text-fg-dim",
          )}
          onClick={onTogglePin}
          aria-label={
            pinned ? `unpin ${prettyChannel(channelKey)}` : `pin ${prettyChannel(channelKey)}`
          }
          title={pinned ? "Unpin from top" : "Pin to top"}
        >
          {pinned ? "★" : "☆"}
        </button>
      )}
      <div className="mb-1 pr-4 text-[0.62rem] uppercase tracking-[0.13em] text-fg-faint">
        {prettyChannel(channelKey)}
      </div>
      <div
        className={cn(
          "font-num text-2xl font-semibold tabular-nums text-fg",
          rendered.bandClass === "band-bad" && "text-bad",
          rendered.bandClass === "band-warn" && "text-warn",
        )}
      >
        {rendered.text}
        {rendered.unit && (
          <span className="ml-1.5 font-num text-[0.7rem] font-medium text-fg-faint">
            {rendered.unit}
          </span>
        )}
      </div>
      {sparkValues.length > 1 && <Sparkline values={sparkValues} width={150} height={30} />}
    </div>
  );
}

function bandUtility(bandClass: RenderedValue["bandClass"]): string {
  if (bandClass === "band-bad") {
    return "border-bad/60 bg-[linear-gradient(180deg,rgba(255,111,111,0.14),rgba(255,111,111,0.04))]";
  }
  if (bandClass === "band-warn") {
    return "border-warn/55 bg-[linear-gradient(180deg,rgba(255,209,102,0.12),rgba(255,209,102,0.03))]";
  }
  return "";
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
  if (!binding || binding.scale === null || snapshot.latest.source !== "decoded") {
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

function isEmptyRawCan(snapshot: ChannelSnapshot): boolean {
  return snapshot.latest.source === "can" && snapshot.latest.value === null;
}

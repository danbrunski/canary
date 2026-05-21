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
import { formatNumber } from "../util/format";
import { writeJson } from "../util/storage";
import { formatAge } from "../util/time";

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
  // Nissan Mode-22 DIDs (characterized in data/dids.yaml). Pinned so a
  // fresh dashboard load shows the values whenever the operator turns
  // on DID polling — keeps them visible alongside the standard PIDs
  // instead of buried in the alphabetical full list.
  "did:ECM_INJECTOR_PW_B1",
  "did:ECM_AF_SENSOR_V_B1",
  "did:ECM_AF_SENSOR_V_B2",
  "did:ECM_THROTTLE_POS_SENSOR_1",
];

const PIN_STORAGE_KEY = "canary.drive.pinned";
const GRAPH_STORAGE_KEY = "canary.drive.graphs";

function loadStoredKeys(storageKey: string): string[] | null {
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (raw === null) return null;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.filter((key): key is string => typeof key === "string")
      : [];
  } catch {
    return null;
  }
}

function loadKeys(storageKey: string): string[] {
  return loadStoredKeys(storageKey) ?? [];
}

/** Per-channel current value + its *own* 2-minute sparkline. There is no
 *  combined multi-channel chart here: these signals span wildly different
 *  scales (rpm vs. a 0-1 flag vs. a 12-digit accumulator) and a shared axis
 *  just squashes everything into a flat line. Each tile auto-scales its own
 *  sparkline; for true multi-channel trends use the dedicated Live ▸ Charts
 *  panel, which normalizes per series. Decoded CAN channels render in
 *  engineering units when their binding has a pinned scale
 *  (``decoded:ECM_RPM`` raw 16376 → "2047 rpm"); values outside a binding's
 *  plausibility bounds are colour-flagged. */
export function TelemetryPanel({ channels, bindings, compact = false }: TelemetryPanelProps) {
  const [query, setQuery] = useState("");
  const [pinned, setPinned] = useState<string[] | null>(() => loadStoredKeys(PIN_STORAGE_KEY));
  const [pinnedOnly, setPinnedOnly] = useState(false);
  // Per-card sparklines are OFF by default (digital readout only); the
  // operator opts a channel's graph in. Persisted per browser.
  const [graphs, setGraphs] = useState<string[]>(() => loadKeys(GRAPH_STORAGE_KEY));

  // `null` ⇒ unseeded; only persist a concrete set (an explicit `[]` is a
  // meaningful "user cleared all pins", which suppresses default re-seeding).
  useEffect(() => {
    if (pinned === null) return;
    writeJson(PIN_STORAGE_KEY, pinned);
  }, [pinned]);

  useEffect(() => {
    writeJson(GRAPH_STORAGE_KEY, graphs);
  }, [graphs]);

  const graphSet = useMemo(() => new Set(graphs), [graphs]);
  const toggleGraph = (key: string) =>
    setGraphs((cur) =>
      cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key],
    );

  const displayChannels = useMemo(() => {
    return [...channels]
      .filter(([_key, snap]) => !isEmptyRawCan(snap))
      .sort(([a], [b]) => a.localeCompare(b));
  }, [channels]);

  // Seed default pins once (only with channels that exist). A decoded
  // replay has none of the OBD defaults → stays empty → equal grid of all.
  useEffect(() => {
    if (pinned !== null || displayChannels.length === 0) return;
    const seed = DEFAULT_PIN_CANDIDATES.filter((k) => channels.has(k));
    if (seed.length > 0) setPinned(seed);
  }, [channels, displayChannels.length, pinned]);

  const activePinned = useMemo(() => pinned ?? [], [pinned]);
  const pinnedSet = useMemo(() => new Set(activePinned), [activePinned]);

  const togglePin = (key: string) =>
    setPinned((cur) => {
      const current = cur ?? [];
      return current.includes(key) ? current.filter((k) => k !== key) : [...current, key];
    });

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
    const pins = activePinned
      .filter((k) => byKey.has(k))
      .map((k) => [k, byKey.get(k)!] as [string, ChannelSnapshot]);
    if (pinnedOnly && pins.length > 0) return pins;
    const rest = visibleChannels.filter(([k]) => !pinnedSet.has(k));
    return [...pins, ...rest];
  }, [visibleChannels, activePinned, pinnedSet, pinnedOnly]);

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
            {compact && activePinned.length > 0 && ` · ${activePinned.length} pinned`}
          </span>
        </div>
        <div className="flex items-center gap-2">
          {compact && activePinned.length > 0 && (
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
                  graphOn={graphSet.has(key)}
                  onToggleGraph={() => toggleGraph(key)}
                />
              ))}
            </div>
          )}
          {activePinned.length === 0 && displayChannels.length > 6 && (
            <p className="mt-2 text-[0.74rem] text-fg-faint">
              Tip: ☆ a channel to pin it to the top — then “pinned only” for a
              focused drive view. Multi-channel trends live in Live ▸ Charts.
            </p>
          )}
        </>
      ) : (
        <div className="mt-1">
          <div className="mb-1.5 flex justify-between gap-2 text-[0.78rem] text-fg-dim">
            <b className="text-fg">Gauge table</b>
            <span>{visibleChannels.length} visible</span>
          </div>
          <div className="max-h-[44rem] overflow-auto rounded-[8px] border border-border-soft bg-black/20">
            <table
              aria-label="Live telemetry values"
              className="w-full min-w-[42rem] border-collapse font-console text-[0.78rem] tabular-nums"
            >
              <thead className="sticky top-0 z-[1] bg-elev text-left text-[0.62rem] uppercase tracking-[0.12em] text-fg-faint">
                <tr>
                  <th scope="col" className="py-1.5 pl-2.5 pr-3 font-semibold">
                    Signal
                  </th>
                  <th scope="col" className="py-1.5 pr-3 font-semibold">
                    Source
                  </th>
                  <th scope="col" className="py-1.5 pr-3 text-right font-semibold">
                    Value
                  </th>
                  <th scope="col" className="py-1.5 pr-3 font-semibold">
                    Unit
                  </th>
                  <th scope="col" className="py-1.5 pr-3 font-semibold">
                    Status
                  </th>
                  <th scope="col" className="py-1.5 pr-2.5 text-right font-semibold">
                    Age
                  </th>
                </tr>
              </thead>
              <tbody>
                {visibleChannels.map(([key, snap]) => (
                  <ChannelTableRow
                    key={key}
                    channelKey={key}
                    snapshot={snap}
                    binding={bindings[key]}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}

function ChannelTableRow({
  channelKey,
  snapshot,
  binding,
}: {
  channelKey: string;
  snapshot: ChannelSnapshot;
  binding?: BindingDisplay;
}) {
  const rendered = renderChannel(snapshot, binding);
  const status = statusMeta(rendered);
  return (
    <tr
      className={cn(
        "border-b border-border-soft align-middle last:border-0 hover:bg-elev-2/65",
        bandRowUtility(rendered.bandClass),
      )}
    >
      <th
        scope="row"
        className="max-w-[20rem] overflow-hidden text-ellipsis whitespace-nowrap py-1.5 pl-2.5 pr-3 text-left font-semibold text-fg"
        title={channelKey}
      >
        {prettyChannel(channelKey)}
      </th>
      <td className="py-1.5 pr-3 text-[0.72rem] uppercase text-fg-dim">
        {snapshot.latest.source}
      </td>
      <td
        className={cn(
          "py-1.5 pr-3 text-right font-bold text-fg",
          rendered.bandClass === "band-bad" && "text-bad",
          rendered.bandClass === "band-warn" && "text-warn",
        )}
      >
        {rendered.text}
      </td>
      <td className="py-1.5 pr-3 text-fg-dim">{rendered.unit || "-"}</td>
      <td className={cn("py-1.5 pr-3 text-[0.72rem] uppercase", status.className)}>
        <span title={status.title}>{status.text}</span>
      </td>
      <td className="py-1.5 pr-2.5 text-right text-fg-dim">
        {formatAge(snapshot.latest.timestamp)}
      </td>
    </tr>
  );
}

/** Fixed sparkline slot height — *always reserved* so toggling a card's
 *  graph on/off never changes the card's size (no grid reflow). */
const GRAPH_SLOT_PX = 30;

function ChannelCard({
  channelKey,
  snapshot,
  binding,
  pinned,
  onTogglePin,
  graphOn = false,
  onToggleGraph,
}: {
  channelKey: string;
  snapshot: ChannelSnapshot;
  binding?: BindingDisplay;
  pinned?: boolean;
  onTogglePin?: () => void;
  graphOn?: boolean;
  onToggleGraph?: () => void;
}) {
  const rendered = renderChannel(snapshot, binding);
  const sparkValues = sparkSeries(snapshot, binding);
  const pretty = prettyChannel(channelKey);
  const canGraph = sparkValues.length > 1;
  return (
    <div
      className={cn(
        "relative rounded-tile border border-border-soft bg-[linear-gradient(180deg,var(--color-elev-2),var(--color-elev))] px-3 py-2 transition-[border-color,box-shadow,transform] duration-150 hover:-translate-y-px hover:border-border hover:shadow-panel",
        pinned && "border-accent/55",
        bandUtility(rendered.bandClass),
      )}
    >
      <div className="absolute right-1.5 top-1.5 flex items-center gap-1.5">
        {onToggleGraph && (
          <button
            type="button"
            className={cn(
              "text-[0.78rem] leading-none transition-colors",
              graphOn ? "text-accent-2" : "text-fg-faint hover:text-fg-dim",
            )}
            onClick={onToggleGraph}
            aria-label={graphOn ? `hide graph for ${pretty}` : `show graph for ${pretty}`}
            title={graphOn ? "Hide sparkline" : "Show sparkline"}
            aria-pressed={graphOn}
          >
            {graphOn ? "▥" : "▤"}
          </button>
        )}
        {onTogglePin && (
          <button
            type="button"
            className={cn(
              "text-[0.8rem] leading-none transition-colors",
              pinned ? "text-accent" : "text-fg-faint hover:text-fg-dim",
            )}
            onClick={onTogglePin}
            aria-label={pinned ? `unpin ${pretty}` : `pin ${pretty}`}
            title={pinned ? "Unpin from top" : "Pin to top"}
          >
            {pinned ? "★" : "☆"}
          </button>
        )}
      </div>
      <div className="mb-1 pr-10 text-[0.62rem] uppercase tracking-[0.13em] text-fg-faint">
        {pretty}
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
        {rendered.raw && (
          <span
            className="ml-1.5 align-middle rounded bg-black/30 px-1 text-[0.55rem] font-bold uppercase tracking-[0.08em] text-fg-faint"
            title="Raw CAN bytes — this signal's scaling/units haven't been reverse-engineered yet (data/bindings.yaml). The number is the raw integer, not an engineering value."
          >
            raw
          </span>
        )}
      </div>
      {/* Slot is always present at a fixed height → constant card size
          whether the graph is on, off, or unavailable. */}
      <div
        className="mt-1 flex items-end"
        style={{ height: GRAPH_SLOT_PX }}
        aria-hidden={!graphOn}
      >
        {graphOn && canGraph && (
          <Sparkline values={sparkValues} width={150} height={GRAPH_SLOT_PX} />
        )}
        {graphOn && !canGraph && (
          <span className="text-[0.66rem] text-fg-faint">no history yet</span>
        )}
      </div>
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

function bandRowUtility(bandClass: RenderedValue["bandClass"]): string {
  if (bandClass === "band-bad") return "bg-bad/10";
  if (bandClass === "band-warn") return "bg-warn/10";
  return "even:bg-white/[0.015]";
}

function statusMeta(rendered: RenderedValue): {
  text: string;
  className: string;
  title: string;
} {
  if (rendered.bandClass === "band-bad") {
    return {
      text: "out",
      className: "text-bad",
      title: "Outside the configured plausible range.",
    };
  }
  if (rendered.bandClass === "band-warn") {
    return {
      text: "edge",
      className: "text-warn",
      title: "Near the edge of the configured plausible range.",
    };
  }
  if (rendered.raw) {
    return {
      text: "raw",
      className: "text-fg-faint",
      title: "Raw CAN integer; scaling and units have not been pinned yet.",
    };
  }
  return {
    text: "ok",
    className: "text-ok",
    title: "Decoded engineering value.",
  };
}

interface RenderedValue {
  text: string;
  unit: string;
  bandClass: "" | "band-warn" | "band-bad";
  /** True when this is the *raw* decoded integer, not an engineering value:
   *  a decoded CAN channel whose binding has no pinned scale (or no binding
   *  at all). The UI flags it so a 12-digit accumulator reads as
   *  "raw, scaling not reverse-engineered yet", not a broken gauge. */
  raw: boolean;
}

/** Apply binding scale/offset/unit, decide whether to color the card. */
export function renderChannel(
  snapshot: ChannelSnapshot,
  binding: BindingDisplay | undefined,
): RenderedValue {
  const raw = snapshot.latest.value;
  if (raw === null || raw === undefined) {
    return { text: "—", unit: "", bandClass: "", raw: false };
  }
  // Non-numeric (VIN string, bool) — pass through, no binding math applies
  if (typeof raw === "string" || typeof raw === "boolean") {
    return { text: String(raw), unit: cleanUnit(snapshot.latest.unit), bandClass: "", raw: false };
  }
  if (!Number.isFinite(raw)) {
    return { text: "—", unit: "", bandClass: "", raw: false };
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
  // A decoded channel with no pinned scale (or no binding at all) is the
  // raw integer from the CAN bytes, not an engineering value.
  const rawDisplay = isDecoded && (!binding || binding.scale === null);
  const unit = cleanUnit(binding?.unit) || cleanUnit(snapshot.latest.unit);
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

  return { text, unit, bandClass, raw: rawDisplay };
}

/** ``unknown`` is a placeholder in bindings.yaml for a not-yet-RE'd unit —
 *  render nothing rather than the literal word "unknown". */
function cleanUnit(unit: string | null | undefined): string {
  if (!unit || unit === "unknown") return "";
  return unit;
}

/** Sparkline series in engineering units when scale is pinned — otherwise
 *  the auto-axis would interpret raw bytes, which looks weird (a coolant
 *  trace bottoming at 53 isn't useful). */
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

function isEmptyRawCan(snapshot: ChannelSnapshot): boolean {
  return snapshot.latest.source === "can" && snapshot.latest.value === null;
}

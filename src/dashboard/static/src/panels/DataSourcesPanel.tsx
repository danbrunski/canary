import { useCallback, useMemo, useState } from "react";

import { getCanCoverage, getDids, getObdCoverage } from "../api/http";
import { Panel } from "../components/Panel";
import { ResourceGate } from "../components/ResourceGate";
import { TableScroll } from "../components/TableScroll";
import { useLiveFullStatus } from "../hooks/statusDemand";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { useControlResource } from "../hooks/useControlResource";
import { streamKey } from "../lib/romMaps";
import { renderChannel } from "../lib/telemetryDisplay";
import { cn, keyValueTableClass, placeholderClass } from "../styles";
import type {
  CanCoverageResponse,
  DidEntry,
  DidMapResponse,
  MemoryEntry,
  ObdCoverageResponse,
  ObdCoverageRow,
  StatusResponse,
} from "../types";
import { formatAltitude, formatGpsSpeed, formatHeading, formatReading } from "../util/format";

type TabId = "obd" | "did" | "memory" | "can" | "gps";

const TABS: { id: TabId; label: string }[] = [
  { id: "obd", label: "OBD PIDs (Mode 01)" },
  { id: "did", label: "Manufacturer DIDs (Mode 22)" },
  { id: "memory", label: "ECU RAM (service 0x23)" },
  { id: "can", label: "CAN broadcast (passive)" },
  { id: "gps", label: "GPS" },
];

interface DataSourcesPanelProps {
  channels: Map<string, ChannelSnapshot>;
  status: StatusResponse | null;
}

/** One combined view of every intake stream, one tab per source: OBD PIDs,
 *  manufacturer DIDs, ECU RAM, broadcast CAN, and GPS. Each tab lists all
 *  known entries for that source; the live "value" column is overlaid from the
 *  telemetry stream (`channels`, keyed `<source>:<channel>`). View controls
 *  hide entries the vehicle doesn't support/isn't sending and float the
 *  active ones to the top. */
export function DataSourcesPanel({ channels, status }: DataSourcesPanelProps) {
  const [tab, setTab] = useState<TabId>("obd");
  const [hideUnavailable, setHideUnavailable] = useState(true);
  const [activeFirst, setActiveFirst] = useState(true);

  const view = { hideUnavailable, activeFirst };

  return (
    <Panel title="Data Sources">
      <div className="mb-3 flex flex-wrap gap-1.5">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={cn(
              "rounded-md px-3 py-1 text-[0.82rem]",
              tab === t.id
                ? "bg-accent/20 text-accent ring-1 ring-accent/40"
                : "text-fg-dim hover:text-fg",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab !== "gps" && tab !== "memory" && (
        <div className="mb-2 flex flex-wrap items-center gap-3 text-[0.74rem] text-fg-dim">
          <Toggle
            label="hide unavailable"
            on={hideUnavailable}
            onClick={() => setHideUnavailable((v) => !v)}
          />
          <Toggle
            label="active on top"
            on={activeFirst}
            onClick={() => setActiveFirst((v) => !v)}
          />
        </div>
      )}

      {tab === "obd" && <ObdTab channels={channels} view={view} />}
      {tab === "did" && <DidTab channels={channels} status={status} view={view} />}
      {tab === "memory" && (
        <MemoryTab channels={channels} status={status} view={{ ...view, hideUnavailable: false }} />
      )}
      {tab === "can" && <CanTab channels={channels} view={view} />}
      {tab === "gps" && <GpsTab status={status} />}
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// shared
// ---------------------------------------------------------------------------

interface ViewOpts {
  hideUnavailable: boolean;
  activeFirst: boolean;
}

const THEAD =
  "sticky top-0 bg-elev text-left text-[0.66rem] uppercase tracking-[0.1em] text-fg-faint";
const SCROLL = "max-h-[30rem] overflow-y-auto";
const TABLE = "w-full border-collapse text-[0.8rem]";
const ROW = "border-b border-border-soft last:border-0";
const TD = "py-1 pr-2";

function Toggle({ label, on, onClick }: { label: string; on: boolean; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className="inline-flex items-center gap-1.5">
      <span
        className={cn(
          "inline-block h-3 w-3 rounded-sm border",
          on ? "border-accent bg-accent/70" : "border-border-soft",
        )}
      />
      {label}
    </button>
  );
}

/** A live channel's current value, formatted with its unit. */
function liveValue(
  channels: Map<string, ChannelSnapshot>,
  key: string | null,
): { text: string; active: boolean } {
  if (!key) return { text: "—", active: false };
  const snap = channels.get(key);
  const value = snap?.latest.value;
  if (value == null || value === "") return { text: "—", active: false };
  const shown =
    typeof value === "number" ? formatReading(value, { trimZeros: true }) : String(value);
  const unit = snap?.latest.unit ? ` ${snap.latest.unit}` : "";
  return { text: `${shown}${unit}`, active: true };
}

function valueCell(active: boolean, text: string) {
  return <span className={active ? "text-fg" : "text-fg-faint"}>{text}</span>;
}

/** Stable filter+sort by an availability/active predicate. */
function arrange<T>(
  rows: T[],
  view: ViewOpts,
  get: (r: T) => { available: boolean; active: boolean },
): T[] {
  let out = rows;
  if (view.hideUnavailable) out = out.filter((r) => get(r).available);
  if (view.activeFirst) {
    out = [...out].sort((a, b) => Number(get(b).active) - Number(get(a).active));
  }
  return out;
}

// ---------------------------------------------------------------------------
// OBD PIDs
// ---------------------------------------------------------------------------

function ObdTab({ channels, view }: { channels: Map<string, ChannelSnapshot>; view: ViewOpts }) {
  const fetcher = useCallback((s: AbortSignal) => getObdCoverage(s), []);
  const { data, error, loading } = useControlResource<ObdCoverageResponse>(fetcher, {
    intervalMs: 15000,
  });

  const rows = useMemo(() => {
    if (!data) return [];
    // Default order: supported first, then by PID — the "29 at the top".
    const base = [...data.rows].sort(
      (a, b) =>
        Number(b.status !== "unsupported") - Number(a.status !== "unsupported") || a.pid - b.pid,
    );
    return arrange(base, view, (r) => {
      const supported = r.status !== "unsupported";
      const active = liveValue(channels, r.channel ? `obd:${r.channel}` : null).active;
      return { available: supported, active };
    });
  }, [data, channels, view]);

  // Custom PIDs (Task 2.6/2.8a) have no PID number in the Mode-01 registry
  // `build_coverage_report` classifies, so they're listed here rather than
  // folded into `rows` — kept out of the "X/Y PIDs supported" summary above,
  // which stays an honest read of the J1979 registry only.
  const customRows = useMemo(() => {
    if (!data) return [];
    return arrange(data.custom_pids, view, (r) => {
      const active = liveValue(channels, `obd:${r.channel}`).active;
      return { available: active, active };
    });
  }, [data, channels, view]);

  return (
    <ResourceGate loading={loading} loadingMessage="probing OBD…" error={error} data={data}>
      {(data) => (
        <>
          <Summary text={`${data.decoded_count}/${data.rows.length} PIDs supported`} />
          <TableScroll className={SCROLL}>
            <table className={TABLE}>
              <thead className={THEAD}>
                <tr>
                  <th className={TD}>PID</th>
                  <th className={TD}>Name</th>
                  <th className={TD}>Value</th>
                </tr>
              </thead>
              <tbody className="font-num tabular-nums">
                {rows.map((r: ObdCoverageRow) => {
                  const lv = liveValue(channels, r.channel ? `obd:${r.channel}` : null);
                  const supported = r.status !== "unsupported";
                  return (
                    <tr key={`${r.mode}-${r.pid}`} className={ROW}>
                      <td className={cn(TD, "text-fg-dim")}>{r.pid_hex}</td>
                      <td className={cn(TD, !supported && "text-fg-faint")}>
                        {r.display_name ?? r.channel ?? "—"}
                      </td>
                      <td className="py-1">{valueCell(lv.active, lv.text)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableScroll>

          {data.custom_pids.length > 0 && (
            <>
              <h3 className="mb-1 mt-3 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
                Custom PIDs ({data.custom_pids.length})
              </h3>
              <TableScroll className={SCROLL}>
                <table className={TABLE}>
                  <thead className={THEAD}>
                    <tr>
                      <th className={TD}>Name</th>
                      <th className={TD}>Value</th>
                    </tr>
                  </thead>
                  <tbody className="font-num tabular-nums">
                    {customRows.map((r) => {
                      const lv = liveValue(channels, `obd:${r.channel}`);
                      return (
                        <tr key={r.name} className={ROW}>
                          <td className={TD}>{r.display_name}</td>
                          <td className="py-1">{valueCell(lv.active, lv.text)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </TableScroll>
            </>
          )}
        </>
      )}
    </ResourceGate>
  );
}

// ---------------------------------------------------------------------------
// Manufacturer DIDs
// ---------------------------------------------------------------------------

/** The catalog unit is what the DID *would* read once its scale constant is
 *  pinned; until then the daemon ships the raw count (unit `raw`). Printing
 *  the catalog unit next to an unscaled count says 28364 ms, so say `raw`
 *  and keep the target unit in the tooltip. */
function unitCell(channels: Map<string, ChannelSnapshot>, d: DidEntry) {
  const liveUnit = channels.get(`did:${d.name}`)?.latest.unit;
  if (liveUnit === "raw") {
    return (
      <span
        className="italic"
        title={`scale constant not pinned in dids.yaml — raw count, not ${d.unit || "engineering units"}`}
      >
        raw
      </span>
    );
  }
  return d.unit || "—";
}

function DidTab({
  channels,
  status,
  view,
}: {
  channels: Map<string, ChannelSnapshot>;
  status: StatusResponse | null;
  view: ViewOpts;
}) {
  const fetcher = useCallback((s: AbortSignal) => getDids(s), []);
  const { data, error, loading } = useControlResource<DidMapResponse>(fetcher, {
    intervalMs: 30000,
  });

  // A capture replaces the default poll selection. The catalog is still the
  // full vehicle inventory, including channels currently not requested.
  const capture = status?.capture_profile;
  const capturing = Boolean(capture?.active);
  const polled = useMemo(
    () => new Set(capture ? (capture?.mode22_enabled ? capture.mode22 : []) : (data?.polled ?? [])),
    [capture, data],
  );

  const rows = useMemo(() => {
    if (!data) return [];
    return arrange(data.dids, view, (d) => {
      const active = liveValue(channels, `did:${d.name}`).active;
      return { available: polled.has(d.name), active };
    });
  }, [data, channels, view, polled]);

  return (
    <ResourceGate loading={loading} loadingMessage="loading DID catalog…" error={error} data={data}>
      {(data) => {
        const responding = data.dids.filter(
          (d) => polled.has(d.name) && liveValue(channels, `did:${d.name}`).active,
        ).length;
        return (
          <>
            <Summary
              text={
                polled.size === 0
                  ? `no DIDs selected (${data.dids.length} catalogued)`
                  : `${responding}/${polled.size} ${capturing ? "capture" : "default"} DIDs responding` +
                    ` (${data.dids.length} catalogued)`
              }
            />
            {data.dids.length === 0 ? (
              <p className={placeholderClass}>
                no DIDs catalogued — pin findings into <code>data/dids.yaml</code>.
              </p>
            ) : (
              <TableScroll className={SCROLL}>
                <table className={TABLE}>
                  <thead className={THEAD}>
                    <tr>
                      <th className={TD}>DID</th>
                      <th className={TD}>Name</th>
                      <th className={TD}>Value</th>
                      <th className={TD}>Unit</th>
                      <th className={TD}>Conf.</th>
                    </tr>
                  </thead>
                  <tbody className="font-num tabular-nums">
                    {rows.map((d: DidEntry) => {
                      const lv = liveValue(channels, `did:${d.name}`);
                      const isPolled = polled.has(d.name);
                      return (
                        <tr key={d.did} className={ROW}>
                          <td className={cn(TD, "text-fg-dim")}>
                            0x{d.did.toString(16).toUpperCase().padStart(4, "0")}
                          </td>
                          <td className={cn(TD, !isPolled && "text-fg-dim")}>{d.name}</td>
                          <td className={TD}>
                            {isPolled ? (
                              valueCell(lv.active, lv.text)
                            ) : (
                              <span
                                className="text-fg-faint italic"
                                title={
                                  capturing
                                    ? "not selected by the active capture"
                                    : "not selected by default collection"
                                }
                              >
                                not selected
                              </span>
                            )}
                          </td>
                          <td className={cn(TD, "text-fg-dim")}>{unitCell(channels, d)}</td>
                          <td className={cn(TD, "text-fg-faint")}>{d.confidence}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </TableScroll>
            )}
          </>
        );
      }}
    </ResourceGate>
  );
}

function MemoryTab({
  channels,
  status,
  view,
}: {
  channels: Map<string, ChannelSnapshot>;
  status: StatusResponse | null;
  view: ViewOpts;
}) {
  const fetcher = useCallback((s: AbortSignal) => getDids(s), []);
  const { data, error, loading } = useControlResource<DidMapResponse>(fetcher, {
    intervalMs: 30000,
  });
  const capture = status?.capture_profile;
  const selected = useMemo(
    () => new Set(capture?.mode22_enabled ? (capture.memory ?? []) : []),
    [capture],
  );
  const rows = useMemo(
    () =>
      arrange(data?.memory ?? [], view, (entry) => ({
        available: selected.has(entry.name),
        active: liveValue(channels, streamKey(`mem:${entry.name}`)).active,
      })),
    [data, view, selected, channels],
  );

  return (
    <ResourceGate loading={loading} loadingMessage="loading RAM catalog…" error={error} data={data}>
      {(catalog) => (
        <>
          <Summary
            text={`${selected.size} selected by current profile (${(catalog.memory ?? []).length} catalogued)`}
          />
          {(catalog.memory ?? []).length === 0 ? (
            <p className={placeholderClass}>No RAM channels catalogued for this vehicle.</p>
          ) : (
            <TableScroll className={SCROLL}>
              <table className={TABLE}>
                <thead className={THEAD}>
                  <tr>
                    <th className={TD}>Address</th>
                    <th className={TD}>Name</th>
                    <th className={TD}>Value</th>
                    <th className={TD}>Unit</th>
                    <th className={TD}>Window</th>
                    <th className={TD}>Conf.</th>
                  </tr>
                </thead>
                <tbody className="font-num tabular-nums">
                  {rows.map((entry: MemoryEntry) => {
                    const live = liveValue(channels, streamKey(`mem:${entry.name}`));
                    const isSelected = selected.has(entry.name);
                    return (
                      <tr key={entry.name} className={ROW}>
                        <td className={cn(TD, "text-fg-dim")}>
                          0x{entry.address.toString(16).toUpperCase().padStart(8, "0")}
                        </td>
                        <td className={cn(TD, !isSelected && "text-fg-dim")}>{entry.name}</td>
                        <td className={TD}>
                          {isSelected ? (
                            valueCell(live.active, live.text)
                          ) : (
                            <span className="text-fg-faint italic">not selected</span>
                          )}
                        </td>
                        <td className={cn(TD, "text-fg-dim")}>{entry.unit || "—"}</td>
                        <td className={cn(TD, "text-fg-dim")}>{entry.window ?? "—"}</td>
                        <td className={cn(TD, "text-fg-faint")}>{entry.confidence}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </TableScroll>
          )}
        </>
      )}
    </ResourceGate>
  );
}

// ---------------------------------------------------------------------------
// CAN broadcast
// ---------------------------------------------------------------------------

interface CanRow {
  canIdHex: string;
  canId: number;
  signal: string;
  source: string;
  channel: string | null;
  undecoded: boolean;
  scale: number | null;
  offset: number | null;
  plausible_min: number | null;
  plausible_max: number | null;
}

function CanTab({ channels, view }: { channels: Map<string, ChannelSnapshot>; view: ViewOpts }) {
  const fetcher = useCallback((s: AbortSignal) => getCanCoverage(s), []);
  const { data, error, loading } = useControlResource<CanCoverageResponse>(fetcher, {
    intervalMs: 30000,
  });

  const rows = useMemo(() => {
    if (!data) return [];
    const out: CanRow[] = data.signals.map((s) => ({
      canIdHex: s.can_id_hex,
      canId: s.can_id,
      signal: s.name,
      source: s.source,
      channel: s.channel,
      undecoded: false,
      scale: s.scale,
      offset: s.offset,
      plausible_min: s.plausible_min,
      plausible_max: s.plausible_max,
    }));
    // Undecoded IDs: raw CAN frames seen on the bus with no decoder.
    const decoded = new Set(data.decoded_ids_hex.map((h) => h.toUpperCase()));
    const seen = new Set<string>();
    for (const key of channels.keys()) {
      if (!key.startsWith("can:")) continue;
      const idHex = key.slice(4).toUpperCase();
      if (decoded.has(idHex) || seen.has(idHex)) continue;
      seen.add(idHex);
      out.push({
        canIdHex: idHex,
        canId: Number.parseInt(idHex, 16) || 0,
        signal: "—",
        source: "raw",
        channel: key,
        undecoded: true,
        scale: null,
        offset: null,
        plausible_min: null,
        plausible_max: null,
      });
    }
    const base = out.sort((a, b) => a.canId - b.canId || a.signal.localeCompare(b.signal));
    return arrange(base, view, (r) => {
      const active = liveValue(channels, r.channel).active;
      // an undecoded ID is "available" when its frame is on the bus
      return { available: active, active };
    });
  }, [data, channels, view]);

  return (
    <ResourceGate loading={loading} loadingMessage="loading CAN catalog…" error={error} data={data}>
      {(data) => (
        <>
          <Summary text={`${data.decoded_id_count} IDs decoded · ${data.signal_count} signals`} />
          <TableScroll className={SCROLL}>
            <table className={TABLE}>
              <thead className={THEAD}>
                <tr>
                  <th className={TD}>CAN ID</th>
                  <th className={TD}>Signal</th>
                  <th className={TD}>Value</th>
                  <th className={TD}>Src</th>
                </tr>
              </thead>
              <tbody className="font-num tabular-nums">
                {rows.map((r) => {
                  const lv = liveValue(channels, r.channel);
                  const snap = r.channel ? channels.get(r.channel) : undefined;
                  const scaled =
                    !r.undecoded && snap
                      ? renderChannel(snap, {
                          name: r.signal,
                          scale: r.scale,
                          offset: r.offset,
                          unit: "",
                          confidence: "medium",
                          plausible_min: r.plausible_min,
                          plausible_max: r.plausible_max,
                        })
                      : null;
                  const valueText = scaled
                    ? `${scaled.text}${scaled.unit ? ` ${scaled.unit}` : ""}`
                    : lv.text;
                  return (
                    <tr key={`${r.canIdHex}-${r.signal}-${r.source}`} className={ROW}>
                      <td className={cn(TD, "text-fg-dim")}>{r.canIdHex}</td>
                      <td className={cn(TD, r.undecoded && "text-fg-faint")}>{r.signal}</td>
                      <td className={TD}>
                        {r.undecoded ? (
                          <span className="text-fg-faint">{lv.active ? "active" : "—"}</span>
                        ) : (
                          valueCell(lv.active, valueText)
                        )}
                      </td>
                      <td className={cn(TD, "text-fg-faint")}>{r.undecoded ? "raw" : r.source}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableScroll>
        </>
      )}
    </ResourceGate>
  );
}

// ---------------------------------------------------------------------------
// GPS
// ---------------------------------------------------------------------------

function GpsTab({ status }: { status: StatusResponse | null }) {
  // The fix is in the full `/status`: fetch it fast while this tab is open.
  useLiveFullStatus();
  const gps = status?.gps ?? null;
  if (!gps) {
    return <p className={placeholderClass}>no GPS configured.</p>;
  }
  const fix = gps.has_fix
    ? `fix · ${gps.num_satellites} sats`
    : gps.num_satellites > 0
      ? `acquiring · ${gps.num_satellites} sats`
      : "no fix";
  const rows: [string, string][] = [
    ["fix", fix],
    ["HDOP", gps.hdop != null ? gps.hdop.toFixed(1) : "—"],
    ["latitude", gps.latitude != null ? `${gps.latitude.toFixed(5)}°` : "—"],
    ["longitude", gps.longitude != null ? `${gps.longitude.toFixed(5)}°` : "—"],
    ["altitude", formatAltitude(gps.altitude)],
    ["speed", formatGpsSpeed(gps.speed)],
    // R08.5: now with the cardinal ("184° S"), like every other heading.
    ["heading", formatHeading(gps.heading)],
    ["fix age", `${gps.age_seconds.toFixed(1)}s`],
  ];
  return (
    <table className={cn(TABLE, keyValueTableClass)}>
      <tbody className="font-num tabular-nums">
        {rows.map(([k, v]) => (
          <tr key={k} className={ROW}>
            <td className={cn(TD, "w-40 text-fg-dim")}>{k}</td>
            <td className="py-1">{v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Summary({ text }: { text: string }) {
  return <p className="mb-2 text-[0.74rem] text-fg-faint">{text}</p>;
}

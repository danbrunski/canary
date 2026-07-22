import { useCallback, useMemo, useState } from "react";

import { getCanCoverage, getDids, getObdCoverage } from "../api/http";
import { Panel } from "../components/Panel";
import { ResourceGate } from "../components/ResourceGate";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { useControlResource } from "../hooks/useControlResource";
import { renderChannel } from "../lib/telemetryDisplay";
import { cn, placeholderClass } from "../styles";
import type {
  CanCoverageResponse,
  DidEntry,
  DidMapResponse,
  ObdCoverageResponse,
  ObdCoverageRow,
  StatusResponse,
} from "../types";

type TabId = "obd" | "did" | "can" | "gps";

const TABS: { id: TabId; label: string }[] = [
  { id: "obd", label: "OBD PIDs" },
  { id: "did", label: "Nissan DIDs" },
  { id: "can", label: "CAN broadcast" },
  { id: "gps", label: "GPS" },
];

interface DataSourcesPanelProps {
  channels: Map<string, ChannelSnapshot>;
  status: StatusResponse | null;
}

/** One combined view of every intake stream, one tab per source (OBD PIDs,
 *  Nissan DIDs, broadcast CAN, GPS). Each tab is a table of *all* known
 *  entries for that source; the live "value" column is overlaid from the
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

      {tab !== "gps" && (
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
      {tab === "did" && <DidTab channels={channels} view={view} />}
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

function Toggle({
  label,
  on,
  onClick,
}: {
  label: string;
  on: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1.5"
    >
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
  const shown = typeof value === "number" ? trimNum(value) : String(value);
  const unit = snap?.latest.unit ? ` ${snap.latest.unit}` : "";
  return { text: `${shown}${unit}`, active: true };
}

function trimNum(n: number): string {
  if (!Number.isFinite(n)) return "—";
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(2).replace(/\.?0+$/, "");
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

function ObdTab({
  channels,
  view,
}: {
  channels: Map<string, ChannelSnapshot>;
  view: ViewOpts;
}) {
  const fetcher = useCallback((s: AbortSignal) => getObdCoverage(s), []);
  const { data, error, loading } = useControlResource<ObdCoverageResponse>(fetcher, {
    intervalMs: 15000,
  });

  const rows = useMemo(() => {
    if (!data) return [];
    // Default order: supported first, then by PID — the "29 at the top".
    const base = [...data.rows].sort(
      (a, b) =>
        Number(b.status !== "unsupported") - Number(a.status !== "unsupported") ||
        a.pid - b.pid,
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
          <div className={SCROLL}>
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
          </div>

          {data.custom_pids.length > 0 && (
            <>
              <h3 className="mb-1 mt-3 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
                Custom PIDs ({data.custom_pids.length})
              </h3>
              <div className={SCROLL}>
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
              </div>
            </>
          )}
        </>
      )}
    </ResourceGate>
  );
}

// ---------------------------------------------------------------------------
// Nissan DIDs
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
  view,
}: {
  channels: Map<string, ChannelSnapshot>;
  view: ViewOpts;
}) {
  const fetcher = useCallback((s: AbortSignal) => getDids(s), []);
  const { data, error, loading } = useControlResource<DidMapResponse>(fetcher, {
    intervalMs: 30000,
  });

  // Only the DIDs in `collection.obd_dids` are ever requested; the rest of
  // the catalog is RE record-keeping. Counting those as "not responding"
  // reads as a fault when nothing is wrong.
  const polled = useMemo(() => new Set(data?.polled ?? []), [data]);

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
                  ? `no DIDs polled (${data.dids.length} catalogued)`
                  : `${responding}/${polled.size} polled DIDs responding` +
                    ` (${data.dids.length} catalogued)`
              }
            />
            {data.dids.length === 0 ? (
              <p className={placeholderClass}>
                no DIDs catalogued — pin findings into <code>data/dids.yaml</code>.
              </p>
            ) : (
              <div className={SCROLL}>
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
                                title="characterized but not in collection.obd_dids — never requested"
                              >
                                not polled
                              </span>
                            )}
                          </td>
                          <td className={cn(TD, "text-fg-dim")}>
                            {unitCell(channels, d)}
                          </td>
                          <td className={cn(TD, "text-fg-faint")}>{d.confidence}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </>
        );
      }}
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

function CanTab({
  channels,
  view,
}: {
  channels: Map<string, ChannelSnapshot>;
  view: ViewOpts;
}) {
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
          <div className={SCROLL}>
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
                  const valueText = scaled ? `${scaled.text}${scaled.unit ? ` ${scaled.unit}` : ""}` : lv.text;
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
          </div>
        </>
      )}
    </ResourceGate>
  );
}

// ---------------------------------------------------------------------------
// GPS
// ---------------------------------------------------------------------------

const KM_PER_MILE = 1.609344;
const M_PER_FT = 0.3048;

function GpsTab({ status }: { status: StatusResponse | null }) {
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
    ["altitude", gps.altitude != null ? `${(gps.altitude / M_PER_FT).toFixed(0)} ft` : "—"],
    ["speed", gps.speed != null ? `${(gps.speed / KM_PER_MILE).toFixed(0)} mph` : "—"],
    ["heading", gps.heading != null ? `${gps.heading.toFixed(0)}°` : "—"],
    ["fix age", `${gps.age_seconds.toFixed(1)}s`],
  ];
  return (
    <table className={TABLE}>
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

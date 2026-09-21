import { useEffect, useRef, useState } from "react";

import { controlJson } from "../api/core";
import { Panel } from "../components/Panel";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { liveValue, mapColor, tracePosition, TRACE_MAX_SKEW_MS } from "../lib/romMaps";
import type { RomInspection, RomTable } from "../lib/romMaps";
import { buttonClass, inputClass } from "../styles";
import type { StatusResponse } from "../types";

interface Props {
  token: string;
  channels: Map<string, ChannelSnapshot>;
  status: StatusResponse | null;
}

export function RomMapsPanel({ token, channels, status }: Props) {
  const [rom, setRom] = useState<RomInspection | null>(null);
  const [filename, setFilename] = useState("");
  const [slot, setSlot] = useState(1);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [now, setNow] = useState(Date.now());
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => { window.clearInterval(timer); request.current?.abort(); };
  }, []);

  async function load(file: File | undefined) {
    if (!file) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setRom(null); setConfirmed(false); setError(""); setFilename(file.name); setLoading(true);
    try {
      if (!file.name.toLowerCase().endsWith(".osirisrom")) throw new Error("Choose an .OsirisROM file.");
      if (!file.size || file.size > 2 * 1024 * 1024) throw new Error("ROM must be nonempty and at most 2 MiB.");
      const data = await controlJson<RomInspection>("/control/rom/inspect", token, {
        method: "POST", headers: { "Content-Type": "application/octet-stream" },
        body: file, signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setRom(data); setSlot(data.map_slots[0]);
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "ROM could not be loaded.");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }

  const rpm = rom ? liveValue(channels, rom.live.rpm, now, rom.afr_stoich) : null;
  const bfs = rom ? liveValue(channels, rom.live.base_fuel_schedule, now, rom.afr_stoich) : null;
  const commanded = rom ? liveValue(channels, rom.live.commanded_afr, now, rom.afr_stoich) : null;
  const vehicle = status?.vehicle_state;
  const stateAge = vehicle?.derived_at ? now - Date.parse(vehicle.derived_at) : NaN;
  const stateFresh = Number.isFinite(stateAge) && stateAge >= -1000 && stateAge <= 15000;
  const fuelCutRow = rom?.live.commanded_afr
    ? channels.get(`${rom.live.commanded_afr.spec}_SENTINEL`)?.latest : null;
  const fuelCut = fuelCutRow?.timestamp && now - Date.parse(fuelCutRow.timestamp) < 2500 && !commanded;
  let traceReason = "";
  if (!confirmed) traceReason = "Confirm the selected ROM and map slot before tracing.";
  else if (!rom?.matches_recorded_revision) traceReason = "Selected ROM differs from the recorded active revision; viewing only.";
  else if (vehicle?.state === "replay") traceReason = "Replay active — live-map tracing is disabled.";
  else if (!stateFresh || vehicle?.state !== "engine_running") traceReason = "Waiting for fresh engine-running status.";
  else if (fuelCut) traceReason = "Fuel-cut sentinel — no active fueling cell inferred.";
  else if (!rpm || !bfs) traceReason = "Waiting for fresh RPM and BFS. Missing/stale channels are not substituted with load or throttle.";
  else if (Math.abs(rpm.timestamp - bfs.timestamp) > TRACE_MAX_SKEW_MS) traceReason = "RPM/BFS sample skew exceeds 750 ms; trace paused.";

  return <Panel title="ROM maps" count="Read-only · no flashing">
    <div className="space-y-5">
      <p className="text-sm text-fg-muted">Load the ROM currently installed in the ECU to inspect its maps. The file is decoded in memory, never stored on the server, and does not change the ECU or active tune configuration.</p>
      <div className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1 text-sm">ROM file
          <input aria-label="ROM file" className={inputClass} type="file" accept=".OsirisROM,.osirisrom" disabled={!token}
            onChange={(event) => { void load(event.target.files?.[0]); event.target.value = ""; }} />
        </label>
        <label className="flex flex-col gap-1 text-sm">Map slot
          <select aria-label="Map slot" className={inputClass} value={slot} disabled={!rom} onChange={(event) => { setSlot(Number(event.target.value)); setConfirmed(false); }}>
            {!rom && <option value={1}>Load a ROM</option>}
            {rom?.map_slots.map((number) => <option key={number} value={number}>Map {number}</option>)}
          </select>
        </label>
        {rom && <button className={buttonClass} onClick={() => { setRom(null); setConfirmed(false); setFilename(""); }}>Unload ROM</button>}
      </div>
      {!token && <p role="status">Enter the dashboard control token to inspect a ROM.</p>}
      {loading && <p role="status">Decoding {filename}…</p>}
      {error && <p role="alert" className="text-red-400">{error}</p>}
      {!rom && !loading && <p className="text-fg-muted">No ROM loaded. Target AFR and Fuel Compensation will appear here; unverified maps are marked TBD.</p>}
      {rom && <>
        <div className="rounded-lg border border-border p-3 text-sm space-y-1">
          <p className="font-semibold">{filename} · {rom.revision}</p>
          <p className="break-all font-mono text-xs">SHA-256 {rom.source_sha256}</p>
          <p>Recorded active revision at upload: {rom.recorded_revision ?? "unknown"}. {rom.matches_recorded_revision ? "File hash matches that record; this is not ECU readback." : "File hash does not match — historical viewing only."}</p>
          <label className="flex items-start gap-2 pt-2"><input type="checkbox" checked={confirmed} disabled={!rom.matches_recorded_revision}
            onChange={(event) => setConfirmed(event.target.checked)} />I confirm this ROM and Map {slot} are currently active in the ECU.</label>
        </div>
        <div className="flex flex-wrap gap-5 font-mono text-sm" aria-label="Live map inputs">
          <span>RPM: {rpm ? rpm.value.toFixed(0) : "unavailable"}</span>
          <span>BFS: {bfs ? `${bfs.value.toFixed(2)} ms` : "unavailable"}</span>
          <span>Final commanded AFR: {commanded ? commanded.value.toFixed(2) : "unavailable / fuel cut"}</span>
        </div>
        <p role="status" className="text-sm text-fg-muted">{traceReason || "Live inferred RPM/BFS lookup. Solid outline = nearest breakpoint; dashed outlines = surrounding cells. ECU interpolation/overrides are not verified."}</p>
        {rom.tables.filter((table) => table.slot === slot).map((table) => <MapTable key={`${slot}-${table.kind}`} table={table}
          position={!traceReason && rpm && bfs ? tracePosition(table, rpm.value, bfs.value) : null}
          outOfRange={!traceReason && !!rpm && !!bfs && table.status === "available" && !tracePosition(table, rpm.value, bfs.value)} />)}
        <ul className="list-disc pl-5 text-xs text-fg-muted space-y-1">{rom.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
      </>}
    </div>
  </Panel>;
}

function MapTable({ table, position, outOfRange }: {
  table: RomTable; position: ReturnType<typeof tracePosition>; outOfRange: boolean;
}) {
  const values = table.values.flat();
  const min = Math.min(...values), max = Math.max(...values);
  return <section aria-label={`Map ${table.slot} ${table.title}`} className="space-y-3">
    <h3 className="text-lg font-semibold">Map {table.slot} · {table.title}</h3>
    <p className="text-xs text-fg-muted">X: {table.x_label} ({table.x_unit}) · Y: {table.y_label} ({table.y_unit}) · Cells: {table.unit}</p>
    {table.status === "tbd" ? <div className="rounded-lg border border-dashed border-border p-6 text-fg-muted">{table.reason} No guessed table values or trace are shown.</div> : <>
      {outOfRange && <p role="status">Operating point is outside the verified axes; no cell highlighted.</p>}
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full border-separate border-spacing-1 text-center font-mono text-sm" aria-label={`Map ${table.slot} ${table.title} values`}>
          <caption className="sr-only">Rows are RPM; columns are {table.x_label}. Colors represent numeric values, not safety.</caption>
          <thead><tr><th scope="col" className="p-2">RPM ↓ / {table.x_unit} →</th>{table.x_axis.map((x, i) => <th scope="col" key={i} className="p-2">{x.toFixed(2)}</th>)}</tr></thead>
          <tbody>{table.values.map((row, r) => <tr key={r}><th scope="row" className="p-2">{table.y_axis[r]}</th>{row.map((value, c) => {
            const nearby = !!position?.rows.includes(r) && !!position.columns.includes(c);
            const primary = position?.row === r && position.column === c;
            return <td key={c} data-trace={primary ? "nearest" : nearby ? "neighbor" : undefined}
              aria-label={`${table.y_axis[r]} RPM, ${table.x_axis[c]} ${table.x_unit}: ${value.toFixed(2)} ${table.unit}${primary ? ", nearest inferred cell" : nearby ? ", surrounding cell" : ""}`}
              title={`Raw ${table.raw[r][c]} · ${value.toFixed(6)} ${table.unit}`}
              className="relative min-w-16 rounded p-3 text-slate-950"
              style={{ backgroundColor: mapColor(value, min, max), outline: nearby ? `${primary ? 3 : 2}px ${primary ? "solid" : "dashed"} #162235` : undefined, outlineOffset: -3, fontWeight: primary ? 800 : 500 }}>
              {primary && <span aria-hidden="true" className="absolute left-1 top-0 text-[10px]">●</span>}{value.toFixed(2)}
            </td>;
          })}</tr>)}</tbody>
        </table>
      </div>
      <div className="flex items-center gap-3 text-xs text-fg-muted"><span>{min.toFixed(2)}</span><span aria-hidden="true" className="h-2 w-40 rounded" style={{ background: `linear-gradient(to right, ${mapColor(min, min, max)}, ${mapColor(min + (max-min)/3, min, max)}, ${mapColor(min + 2*(max-min)/3, min, max)}, ${mapColor(max, min, max)})` }} /><span>{max.toFixed(2)} {table.unit}</span><span>Low → high; not a safety rating</span></div>
    </>}
  </section>;
}

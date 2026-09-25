import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { getCaptureProfiles } from "../api/http";
import { inspectRom } from "../api/workbench";
import { Panel } from "../components/Panel";
import { describeError, useControlResource } from "../hooks/useControlResource";
import { tableKey, useCoverageOverlay, type CoverageSource } from "../hooks/useCoverageOverlay";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { LoadedRom } from "../hooks/useTuningContext";
import { liveValue, streamRow, tracePosition } from "../lib/romMaps";
import type { RomInspection, RomTable } from "../lib/romMaps";
import {
  cellDisagreement,
  describeDisagreement,
  ecmPoint,
  ecmPosition,
  mappingRefusal,
  traceGate,
  traceLiveness,
} from "../lib/romTrace";
import type { CellPosition, TraceLiveness } from "../lib/romTrace";
import { buttonClass, inputClass } from "../styles";
import type { StatusResponse, WorkbenchItem } from "../types";
import { MapTable } from "./MapTable";

interface Props {
  token: string;
  channels: Map<string, ChannelSnapshot>;
  status: StatusResponse | null;
  /** Engine Tuning group state (W02.5): when given, the parsed ROM lives
   *  there and survives navigation between the group's pages. Never
   *  persisted to storage. Omitted, the panel holds it itself. */
  loaded?: LoadedRom | null;
  onLoadedChange?: (rom: LoadedRom | null) => void;
  /** The log picked on Logs (W02.5) — the coverage overlay's subject. */
  selectedLogId?: string | null;
  onNavigate?: (item: WorkbenchItem) => void;
}

export function RomMapsPanel({
  token,
  channels,
  status,
  loaded,
  onLoadedChange,
  selectedLogId = null,
  onNavigate,
}: Props) {
  const [ownRom, setOwnRom] = useState<LoadedRom | null>(null);
  const current = loaded !== undefined ? loaded : ownRom;
  const setCurrent = onLoadedChange ?? setOwnRom;
  const rom: RomInspection | null = current?.inspection ?? null;
  const [pendingName, setPendingName] = useState("");
  const filename = current?.filename ?? pendingName;
  const [slot, setSlot] = useState(() => current?.inspection.map_slots[0] ?? 1);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [tally, setTally] = useState<
    Record<string, { checks: number; disagreements: number; streakSince: number | null }>
  >({});
  const [drive, setDrive] = useState(false);
  const lastEcmSample = useRef<Record<string, number>>({});
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => {
      window.clearInterval(timer);
      request.current?.abort();
    };
  }, []);

  // The vehicle's capture-profile catalog, to name a profile that carries the
  // trace channels when the active one does not (W05.4).
  const hasRom = !!rom;
  const catalog = useControlResource(
    useCallback((signal: AbortSignal) => getCaptureProfiles(token, signal), [token]),
    { enabled: Boolean(token) && hasRom, intervalMs: null },
  );
  const profiles = catalog.data
    ? Array.isArray(catalog.data.profiles)
      ? catalog.data.profiles
      : []
    : null;

  async function load(file: File | undefined) {
    if (!file) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setCurrent(null);
    setConfirmed(false);
    setError("");
    setPendingName(file.name);
    setLoading(true);
    try {
      if (!file.name.toLowerCase().endsWith(".osirisrom"))
        throw new Error("Choose an .OsirisROM file.");
      if (!file.size || file.size > 2 * 1024 * 1024)
        throw new Error("ROM must be nonempty and at most 2 MiB.");
      const data = await inspectRom(token, file, controller.signal);
      if (controller.signal.aborted) return;
      setCurrent({ filename: file.name, inspection: data });
      setSlot(data.map_slots[0]);
    } catch (err) {
      if (!controller.signal.aborted) setError(describeError(err));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }

  const rpm = rom ? liveValue(channels, rom.live.rpm, now, rom.afr_stoich) : null;
  const bfs = rom ? liveValue(channels, rom.live.base_fuel_schedule, now, rom.afr_stoich) : null;
  const commanded = rom ? liveValue(channels, rom.live.commanded_afr, now, rom.afr_stoich) : null;
  const fuelCutRow = rom?.live.commanded_afr
    ? streamRow(channels, `${rom.live.commanded_afr.spec}_SENTINEL`)
    : null;
  const fuelCut =
    !!fuelCutRow?.timestamp && now - Date.parse(fuelCutRow.timestamp) < 2500 && !commanded;
  const { gate: gateReason, trace: traceReason } = traceGate({
    confirmed,
    rom,
    status,
    now,
    fuelCut,
    rpm,
    bfs,
  });

  const captureActive = !!status?.capture_profile?.active;
  const captureKey = captureActive
    ? `${status?.capture_profile?.name}|${status?.capture_profile?.started_at}`
    : null;
  const slotTables = useMemo(
    () => (rom ? rom.tables.filter((table) => table.slot === slot) : []),
    [rom, slot],
  );
  const overlay = useCoverageOverlay({ token, selectedLogId, captureKey, tables: slotTables });

  // Per table: the ECM cell (W05.2), and whether the trace is live (W05.4).
  const ecm = new Map<
    string,
    {
      position: CellPosition | null;
      at: { x: number; y: number } | null;
      liveness: TraceLiveness | null;
      reason: string;
      xTimestamp: number | null;
    }
  >();
  for (const table of slotTables) {
    if (table.status !== "available" || !table.trace) continue;
    const liveness = traceLiveness(table.trace, channels, now, status, profiles);
    const refusal = mappingRefusal(table.trace);
    const point = ecmPoint(table.trace, channels, now);
    let reason =
      gateReason ||
      refusal ||
      (liveness.state !== "live" ? "ECM trace greyed out (see above)." : "");
    const position = !reason && point ? ecmPosition(table, point) : null;
    if (!reason && !point) reason = "Waiting for paired X/Y trace samples within 750 ms.";
    else if (!reason && !position)
      reason = "ECM coordinates are outside this table's axes; no ECM cell outlined.";
    ecm.set(tableKey(table), {
      position,
      at: point ? { x: point.x, y: point.y } : null,
      liveness,
      reason,
      xTimestamp: point?.xTimestamp ?? null,
    });
  }

  // One step per new ECM sample: the disagreement tally (W05.3) and, during
  // a capture, the client-side live coverage (W05.8).
  useEffect(() => {
    for (const table of slotTables) {
      const key = tableKey(table);
      const state = ecm.get(key);
      if (
        !state?.position ||
        state.xTimestamp === null ||
        lastEcmSample.current[key] === state.xTimestamp
      )
        continue;
      lastEcmSample.current[key] = state.xTimestamp;
      const inferred =
        !traceReason && rpm && bfs ? tracePosition(table, rpm.value, bfs.value) : null;
      if (inferred) {
        const differs =
          inferred.row !== state.position.row || inferred.column !== state.position.column;
        setTally((prev) => {
          const old = prev[key] ?? { checks: 0, disagreements: 0, streakSince: null };
          return {
            ...prev,
            [key]: {
              checks: old.checks + 1,
              disagreements: old.disagreements + (differs ? 1 : 0),
              streakSince: differs ? (old.streakSince ?? state.xTimestamp) : null,
            },
          };
        });
      }
      overlay.recordLive(table, state.position);
    }
  });
  function renderTable(table: RomTable, large = false) {
    const key = tableKey(table);
    const inferred = !traceReason && rpm && bfs ? tracePosition(table, rpm.value, bfs.value) : null;
    const state = ecm.get(key);
    const disagreement =
      state?.position && inferred && rpm && bfs && state.at
        ? cellDisagreement(state.position, inferred, state.at, { x: bfs.value, y: rpm.value })
        : null;
    const counts = tally[key];
    const coverage = overlay.view(table);
    const source = overlay.sourceFor(table);
    return (
      <div key={key} className="space-y-2">
        {table.status === "available" && table.trace && state && (
          <div
            className="space-y-1 text-sm"
            aria-label={`ECM trace Map ${table.slot} ${table.title}`}
          >
            <p
              role="status"
              data-trace-liveness={state.liveness?.state}
              className={state.liveness?.state === "live" ? "" : "text-fg-dim opacity-70"}
            >
              {state.liveness?.state === "live"
                ? `ECM trace live: ${table.trace.x.channel} × ${table.trace.y.channel}.`
                : state.liveness?.message}
              {state.liveness?.state === "not_polled" &&
                state.liveness.profiles.length > 0 &&
                onNavigate && (
                  <>
                    {" "}
                    <button className={buttonClass} onClick={() => onNavigate("tuning-logging")}>
                      Open Logging
                    </button>
                  </>
                )}
            </p>
            <p className="text-fg-dim">
              {state.position
                ? "ECM cell: double ring — the cell the ECM's own trace coordinates point at."
                : state.reason}
            </p>
            {disagreement && (
              <p role="alert" className="text-warn">
                {describeDisagreement(disagreement, table)} A persistent disagreement is a finding:
                a wrong axis decode, a wrong BFS scale, or interpolation the inference does not
                model.
              </p>
            )}
            {state.position && inferred && !disagreement && (
              <p>ECM cell and inferred cell agree.</p>
            )}
            {counts && counts.checks > 0 && (
              <p className="text-xs text-fg-dim">
                Disagreed on {counts.disagreements} of {counts.checks} ECM samples since tracing
                began
                {counts.streakSince !== null
                  ? `; current disagreement for ${((now - counts.streakSince) / 1000).toFixed(0)} s`
                  : ""}
                .
              </p>
            )}
          </div>
        )}
        {table.status === "available" && (
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                aria-label={`Coverage overlay Map ${table.slot} ${table.title}`}
                checked={overlay.isOn(table)}
                onChange={(event) => overlay.toggle(table, event.target.checked)}
              />
              Coverage overlay
            </label>
            {overlay.isOn(table) && (
              <select
                aria-label={`Coverage source Map ${table.slot} ${table.title}`}
                className={inputClass}
                value={source}
                onChange={(event) => overlay.choose(table, event.target.value as CoverageSource)}
              >
                <option value="log">
                  Selected log{selectedLogId ? ` (${selectedLogId})` : ""}
                </option>
                <option value="live">This capture (live)</option>
              </select>
            )}
          </div>
        )}
        {coverage && (
          <p role="status" className="text-sm text-fg-dim">
            {coverage.note}
            {coverage.action === "logs" && onNavigate && (
              <>
                {" "}
                <button className={buttonClass} onClick={() => onNavigate("tuning-logs")}>
                  Open Logs
                </button>
              </>
            )}
          </p>
        )}
        <MapTable
          table={table}
          position={inferred}
          ecm={state?.position ?? null}
          coverage={coverage?.grid ?? null}
          dwell={coverage?.dwell ?? null}
          large={large}
          outOfRange={!traceReason && !!rpm && !!bfs && table.status === "available" && !inferred}
        />
      </div>
    );
  }

  const driveTable = slotTables.find((table) => table.status === "available") ?? null;

  return (
    <Panel title="ROM maps" count="Read-only · no flashing">
      <div className="space-y-5">
        <p className="text-sm text-fg-dim">
          Load the ROM currently installed in the ECU to inspect its maps. The file is decoded in
          memory, never stored on the server, and does not change the ECU or active tune
          configuration.
        </p>
        <div className="flex flex-wrap items-end gap-4">
          <label className="flex flex-col gap-1 text-sm">
            ROM file
            <input
              aria-label="ROM file"
              className={inputClass}
              type="file"
              accept=".OsirisROM,.osirisrom"
              disabled={!token}
              onChange={(event) => {
                void load(event.target.files?.[0]);
                event.target.value = "";
              }}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Map slot
            <select
              aria-label="Map slot"
              className={inputClass}
              value={slot}
              disabled={!rom}
              onChange={(event) => {
                setSlot(Number(event.target.value));
                setConfirmed(false);
              }}
            >
              {!rom && <option value={1}>Load a ROM</option>}
              {rom?.map_slots.map((number) => (
                <option key={number} value={number}>
                  Map {number}
                </option>
              ))}
            </select>
          </label>
          {rom && (
            <button
              className={buttonClass}
              onClick={() => {
                setCurrent(null);
                setConfirmed(false);
                setPendingName("");
              }}
            >
              Unload ROM
            </button>
          )}
          {driveTable && (
            <button className={buttonClass} onClick={() => setDrive(true)}>
              Drive presentation
            </button>
          )}
        </div>
        {!token && <p role="status">Enter the dashboard control token to inspect a ROM.</p>}
        {loading && <p role="status">Decoding {filename}…</p>}
        {error && (
          <p role="alert" className="text-bad">
            {error}
          </p>
        )}
        {!rom && !loading && (
          <p className="text-fg-dim">
            No ROM loaded. Target AFR and Fuel Compensation will appear here; unverified maps are
            marked TBD.
          </p>
        )}
        {rom && (
          <>
            <div className="rounded-lg border border-border p-3 text-sm space-y-1">
              <p className="font-semibold">
                {filename} · {rom.revision}
              </p>
              <p className="break-all font-mono text-xs">SHA-256 {rom.source_sha256}</p>
              <p>
                Recorded active revision at upload: {rom.recorded_revision ?? "unknown"}.{" "}
                {rom.matches_recorded_revision
                  ? "File hash matches that record; this is not ECU readback."
                  : "File hash does not match — historical viewing only."}
              </p>
              {status?.calibration_matches_registry === true && (
                <p>
                  ECU calibration id {status.ecu_calibration_id} matches the flash history's running
                  revision.
                </p>
              )}
              {status?.calibration_matches_registry == null && (
                <p className="text-fg-dim">
                  ECU calibration id not read yet; the slot assertion below is the only check that
                  this ROM is running.
                </p>
              )}
              <label className="flex items-start gap-2 pt-2">
                <input
                  type="checkbox"
                  checked={confirmed}
                  disabled={!rom.matches_recorded_revision}
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                I confirm this ROM and Map {slot} are currently active in the ECU.
              </label>
            </div>
            <div className="flex flex-wrap gap-5 font-mono text-sm" aria-label="Live map inputs">
              <span>RPM: {rpm ? rpm.value.toFixed(0) : "unavailable"}</span>
              <span>BFS: {bfs ? `${bfs.value.toFixed(2)} ms` : "unavailable"}</span>
              <span>
                Final commanded AFR:{" "}
                {commanded ? commanded.value.toFixed(2) : "unavailable / fuel cut"}
              </span>
            </div>
            <p role="status" className="text-sm text-fg-dim">
              {traceReason ||
                "Live inferred RPM/BFS lookup. Solid outline = nearest breakpoint; dashed outlines = surrounding cells. ECU interpolation/overrides are not verified."}
            </p>
            {slotTables.map((table) => (
              <section
                key={tableKey(table)}
                aria-label={`Map ${table.slot} ${table.title}`}
                className="space-y-3"
              >
                <h3 className="text-lg font-semibold">
                  Map {table.slot} · {table.title}
                </h3>
                {renderTable(table)}
              </section>
            ))}
            <ul className="list-disc pl-5 text-xs text-fg-dim space-y-1">
              {rom.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </>
        )}
        {drive && driveTable && (
          <div
            role="dialog"
            aria-label="Drive presentation"
            className="fixed inset-0 z-50 space-y-3 overflow-auto bg-bg p-4 text-lg"
          >
            <div className="flex items-center gap-3">
              <h3 className="mr-auto text-2xl font-semibold">
                Map {driveTable.slot} · {driveTable.title}
              </h3>
              <button className={buttonClass} onClick={() => setDrive(false)}>
                Exit drive presentation
              </button>
            </div>
            <p role="status" className="text-fg-dim">
              {traceReason || "Inferred lookup live."}
            </p>
            {renderTable(driveTable, true)}
          </div>
        )}
      </div>
    </Panel>
  );
}

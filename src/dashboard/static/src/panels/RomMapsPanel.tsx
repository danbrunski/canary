import { useEffect, useRef, useState } from "react";

import { controlJson } from "../api/core";
import { Panel } from "../components/Panel";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { liveValue, mapColor, tracePosition, TRACE_MAX_SKEW_MS } from "../lib/romMaps";
import type { RomInspection, RomTable } from "../lib/romMaps";
import {
  addCoverage,
  cellDisagreement,
  coverageClass,
  coverageColor,
  describeDisagreement,
  ecmPoint,
  ecmPosition,
  emptyCoverage,
  mappingRefusal,
  residencyMatchesTable,
  THIN_COVERAGE_SAMPLES,
  traceLiveness,
} from "../lib/romTrace";
import type { CellPosition, CellResidency, CoverageGrid, TraceLiveness } from "../lib/romTrace";
import { describeRefusal } from "../lib/workbenchApi";
import { buttonClass, inputClass } from "../styles";
import type {
  CaptureProfile,
  CaptureProfilesResponse,
  StatusResponse,
  WorkbenchItem,
} from "../types";
import type { LoadedRom } from "../hooks/useTuningContext";

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

type CoverageSource = "log" | "live";
type ResidencyState =
  { loading: true } | { loading: false; result: CellResidency } | { loading: false; error: string };

const tableKey = (table: RomTable) => `${table.slot}-${table.kind}`;

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
  const [now, setNow] = useState(Date.now());
  const [profiles, setProfiles] = useState<CaptureProfile[] | null>(null);
  const [coverageOn, setCoverageOn] = useState<Record<string, boolean>>({});
  const [coverageSource, setCoverageSource] = useState<Record<string, CoverageSource>>({});
  const [residency, setResidency] = useState<Record<string, ResidencyState>>({});
  const [liveCoverage, setLiveCoverage] = useState<Record<string, CoverageGrid>>({});
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
  useEffect(() => {
    if (!token || !hasRom) return;
    const controller = new AbortController();
    controlJson<CaptureProfilesResponse>("/control/capture/profiles", token, {
      signal: controller.signal,
    })
      .then((data) => {
        if (!controller.signal.aborted)
          setProfiles(Array.isArray(data?.profiles) ? data.profiles : []);
      })
      .catch(() => {
        if (!controller.signal.aborted) setProfiles(null);
      });
    return () => controller.abort();
  }, [token, hasRom]);

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
      const data = await controlJson<RomInspection>("/control/rom/inspect", token, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setCurrent({ filename: file.name, inspection: data });
      setSlot(data.map_slots[0]);
    } catch (err) {
      if (!controller.signal.aborted)
        setError(err instanceof Error ? err.message : "ROM could not be loaded.");
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
    ? (
        channels.get(`${rom.live.commanded_afr.spec}_SENTINEL`) ??
        channels.get(`obd:${rom.live.commanded_afr.spec}_SENTINEL`)
      )?.latest
    : null;
  const fuelCut =
    fuelCutRow?.timestamp && now - Date.parse(fuelCutRow.timestamp) < 2500 && !commanded;
  // Gates shared by both traces: nothing is called an active cell unless the
  // operator asserts this ROM and slot are running, and nothing the daemon
  // knows contradicts it (W05.5).
  let gateReason = "";
  if (!confirmed) gateReason = "Confirm the selected ROM and map slot before tracing.";
  else if (!rom?.matches_recorded_revision)
    gateReason = "Selected ROM differs from the recorded active revision; viewing only.";
  else if (status?.calibration_matches_registry === false)
    gateReason = `ECU reports calibration id ${status.ecu_calibration_id ?? "unknown"}, which does not match the flash history's ${status.registry_revision ?? "running revision"} (${status.registry_calibration_id ?? "no recorded id"}). The ECU is not running the recorded ROM, so no trace is called active.`;
  else if (
    status?.registry_revision &&
    rom.recorded_revision &&
    status.registry_revision !== rom.recorded_revision
  )
    gateReason = `The flash history's running revision is now ${status.registry_revision}, not this ROM's ${rom.recorded_revision}; no trace is called active.`;
  else if (vehicle?.state === "replay")
    gateReason = "Replay active — live-map tracing is disabled.";
  else if (!stateFresh || vehicle?.state !== "engine_running")
    gateReason = "Waiting for fresh engine-running status.";
  let traceReason = gateReason;
  if (!traceReason && fuelCut) traceReason = "Fuel-cut sentinel — no active fueling cell inferred.";
  else if (!traceReason && (!rpm || !bfs))
    traceReason =
      "Waiting for fresh RPM and BFS. Missing/stale channels are not substituted with load or throttle.";
  else if (
    !traceReason &&
    rpm &&
    bfs &&
    Math.abs(rpm.timestamp - bfs.timestamp) > TRACE_MAX_SKEW_MS
  )
    traceReason = "RPM/BFS sample skew exceeds 750 ms; trace paused.";

  const captureActive = !!status?.capture_profile?.active;
  const captureKey = captureActive
    ? `${status?.capture_profile?.name}|${status?.capture_profile?.started_at}`
    : null;
  const slotTables = rom ? rom.tables.filter((table) => table.slot === slot) : [];

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
      if (captureKey) {
        const position = state.position;
        setLiveCoverage((prev) => ({
          ...prev,
          [key]: addCoverage(prev[key] ?? emptyCoverage(table), position),
        }));
      }
    }
  });
  // Live coverage belongs to one capture: reset when it ends or another starts.
  useEffect(() => {
    setLiveCoverage({});
  }, [captureKey]);

  async function fetchResidency(table: RomTable, logId: string) {
    const key = `${logId}|${table.kind}`;
    setResidency((prev) => ({ ...prev, [key]: { loading: true } }));
    try {
      const result = await controlJson<CellResidency>(
        `/control/sessions/${encodeURIComponent(logId)}/cell-residency?table=${encodeURIComponent(table.kind)}`,
        token,
      );
      setResidency((prev) => ({ ...prev, [key]: { loading: false, result } }));
    } catch (err) {
      const refusal = describeRefusal(err);
      const message =
        refusal.kind === "deferred"
          ? `Coverage for a stored log is deferred while “${refusal.profile ?? "a capture"}” is recording. ${refusal.laptopPath ?? ""}`.trim()
          : refusal.message;
      setResidency((prev) => ({ ...prev, [key]: { loading: false, error: message } }));
    }
  }

  function toggleCoverage(table: RomTable, on: boolean) {
    const key = tableKey(table);
    setCoverageOn((prev) => ({ ...prev, [key]: on }));
    const source = coverageSource[key] ?? (captureActive ? "live" : "log");
    setCoverageSource((prev) => ({ ...prev, [key]: source }));
    if (on && source === "log" && selectedLogId && token) void fetchResidency(table, selectedLogId);
  }

  function chooseSource(table: RomTable, source: CoverageSource) {
    setCoverageSource((prev) => ({ ...prev, [tableKey(table)]: source }));
    if (source === "log" && selectedLogId && token) void fetchResidency(table, selectedLogId);
  }

  // Refetch when the selected log changes under an open overlay.
  useEffect(() => {
    if (!selectedLogId || !token) return;
    for (const table of slotTables) {
      const key = tableKey(table);
      if (
        coverageOn[key] &&
        (coverageSource[key] ?? "log") === "log" &&
        !residency[`${selectedLogId}|${table.kind}`]
      )
        void fetchResidency(table, selectedLogId);
    }
  }, [selectedLogId]);

  function coverageFor(table: RomTable): {
    grid: CoverageGrid | null;
    dwell: number[][] | null;
    note: string;
    action?: "logs";
  } {
    const key = tableKey(table);
    const source = coverageSource[key] ?? (captureActive ? "live" : "log");
    if (source === "live") {
      if (!captureActive)
        return {
          grid: null,
          dwell: null,
          note: "No capture is recording; live coverage accumulates only during a capture and resets when it ends.",
        };
      const refusal = mappingRefusal(table.trace);
      if (refusal)
        return { grid: null, dwell: null, note: `Live coverage unavailable. ${refusal}` };
      const grid = liveCoverage[key] ?? emptyCoverage(table);
      const total = grid.flat().reduce((a, b) => a + b, 0);
      return {
        grid,
        dwell: null,
        note: `Live coverage for this capture: ${total} ECM samples. Client-side only; resets when the capture ends.`,
      };
    }
    if (!selectedLogId)
      return {
        grid: null,
        dwell: null,
        note: "No log selected. Choose one on Logs to see which cells it visited.",
        action: "logs",
      };
    const state = residency[`${selectedLogId}|${table.kind}`];
    if (!state || state.loading)
      return { grid: null, dwell: null, note: `Reading cell residency for ${selectedLogId}…` };
    if ("error" in state) return { grid: null, dwell: null, note: state.error };
    const result = state.result;
    if (!result.available)
      return {
        grid: null,
        dwell: null,
        note: `No coverage for ${selectedLogId}: ${result.message}`,
      };
    if (!residencyMatchesTable(result, table))
      return {
        grid: null,
        dwell: null,
        note: `${selectedLogId} ran revision ${result.revision ?? "unknown"}, whose axes differ from the loaded ROM's; its coverage is not overlaid on this table.`,
      };
    return {
      grid: result.counts,
      dwell: result.dwell_s,
      note: `${selectedLogId}: ${result.samples} ECM samples${result.outside_axes ? `, ${result.outside_axes} outside the axes` : ""}.`,
    };
  }

  function renderTable(table: RomTable, large = false) {
    const key = tableKey(table);
    const inferred = !traceReason && rpm && bfs ? tracePosition(table, rpm.value, bfs.value) : null;
    const state = ecm.get(key);
    const disagreement =
      state?.position && inferred && rpm && bfs && state.at
        ? cellDisagreement(state.position, inferred, state.at, { x: bfs.value, y: rpm.value })
        : null;
    const counts = tally[key];
    const coverage = coverageOn[key] && table.status === "available" ? coverageFor(table) : null;
    const source = coverageSource[key] ?? (captureActive ? "live" : "log");
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
              className={state.liveness?.state === "live" ? "" : "text-fg-muted opacity-70"}
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
            <p className="text-fg-muted">
              {state.position
                ? "ECM cell: double ring — the cell the ECM's own trace coordinates point at."
                : state.reason}
            </p>
            {disagreement && (
              <p role="alert" className="text-amber-400">
                {describeDisagreement(disagreement, table)} A persistent disagreement is a finding:
                a wrong axis decode, a wrong BFS scale, or interpolation the inference does not
                model.
              </p>
            )}
            {state.position && inferred && !disagreement && (
              <p>ECM cell and inferred cell agree.</p>
            )}
            {counts && counts.checks > 0 && (
              <p className="text-xs text-fg-muted">
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
                checked={!!coverageOn[key]}
                onChange={(event) => toggleCoverage(table, event.target.checked)}
              />
              Coverage overlay
            </label>
            {coverageOn[key] && (
              <select
                aria-label={`Coverage source Map ${table.slot} ${table.title}`}
                className={inputClass}
                value={source}
                onChange={(event) => chooseSource(table, event.target.value as CoverageSource)}
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
          <p role="status" className="text-sm text-fg-muted">
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
        <p className="text-sm text-fg-muted">
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
          <p role="alert" className="text-red-400">
            {error}
          </p>
        )}
        {!rom && !loading && (
          <p className="text-fg-muted">
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
                <p className="text-fg-muted">
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
            <p role="status" className="text-sm text-fg-muted">
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
            <ul className="list-disc pl-5 text-xs text-fg-muted space-y-1">
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
            <p role="status" className="text-fg-muted">
              {traceReason || "Inferred lookup live."}
            </p>
            {renderTable(driveTable, true)}
          </div>
        )}
      </div>
    </Panel>
  );
}

function MapTable({
  table,
  position,
  ecm,
  coverage,
  dwell,
  outOfRange,
  large,
}: {
  table: RomTable;
  position: CellPosition | null;
  ecm: CellPosition | null;
  coverage: CoverageGrid | null;
  dwell: number[][] | null;
  outOfRange: boolean;
  large: boolean;
}) {
  const values = table.values.flat();
  const min = Math.min(...values),
    max = Math.max(...values);
  const maxCount = coverage ? Math.max(0, ...coverage.flat()) : 0;
  return (
    <div className="space-y-3">
      <p className="text-xs text-fg-muted">
        X: {table.x_label} ({table.x_unit}) · Y: {table.y_label} ({table.y_unit}) · Cells:{" "}
        {table.unit}
      </p>
      {table.status === "tbd" ? (
        <div className="rounded-lg border border-dashed border-border p-6 text-fg-muted">
          {table.reason} No guessed table values or trace are shown.
        </div>
      ) : (
        <>
          {outOfRange && (
            <p role="status">Operating point is outside the verified axes; no cell highlighted.</p>
          )}
          <div className="overflow-x-auto rounded-lg border border-border">
            <table
              className={`w-full border-separate border-spacing-1 text-center font-mono ${large ? "text-xl" : "text-sm"}`}
              aria-label={`Map ${table.slot} ${table.title} values`}
            >
              <caption className="sr-only">
                Rows are RPM; columns are {table.x_label}. Colors represent{" "}
                {coverage ? "sample counts from the coverage overlay" : "numeric values"}, not
                safety.
              </caption>
              <thead>
                <tr>
                  <th scope="col" className="p-2">
                    RPM ↓ / {table.x_unit} →
                  </th>
                  {table.x_axis.map((x, i) => (
                    <th scope="col" key={i} className="p-2">
                      {x.toFixed(2)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {table.values.map((row, r) => (
                  <tr key={r}>
                    <th scope="row" className="p-2">
                      {table.y_axis[r]}
                    </th>
                    {row.map((value, c) => {
                      const nearby = !!position?.rows.includes(r) && !!position.columns.includes(c);
                      const primary = position?.row === r && position.column === c;
                      const ecmCell = ecm?.row === r && ecm.column === c;
                      const count = coverage?.[r]?.[c];
                      const covered = count === undefined ? null : coverageClass(count);
                      const coverageText =
                        count === undefined
                          ? ""
                          : `, ${count} samples${dwell ? `, ${(dwell[r]?.[c] ?? 0).toFixed(1)} s dwell` : ""}${covered === "unvisited" ? " (never visited)" : covered === "thin" ? " (thin)" : ""}`;
                      return (
                        <td
                          key={c}
                          data-trace={primary ? "nearest" : nearby ? "neighbor" : undefined}
                          data-ecm={ecmCell ? "cell" : undefined}
                          data-coverage={covered ?? undefined}
                          aria-label={`${table.y_axis[r]} RPM, ${table.x_axis[c]} ${table.x_unit}: ${value.toFixed(2)} ${table.unit}${primary ? ", nearest inferred cell" : nearby ? ", surrounding cell" : ""}${ecmCell ? ", ECM cell" : ""}${coverageText}`}
                          title={`Raw ${table.raw[r][c]} · ${value.toFixed(6)} ${table.unit}${count === undefined ? "" : ` · ${count} samples${dwell ? ` · ${(dwell[r]?.[c] ?? 0).toFixed(1)} s` : ""}`}`}
                          className={`relative rounded text-slate-950 ${large ? "min-w-24 p-5" : "min-w-16 p-3"}`}
                          style={{
                            backgroundColor:
                              count === undefined
                                ? mapColor(value, min, max)
                                : coverageColor(count, maxCount),
                            backgroundImage:
                              covered === "unvisited"
                                ? "repeating-linear-gradient(45deg, transparent 0 6px, rgba(100,116,139,0.25) 6px 8px)"
                                : undefined,
                            outline: nearby
                              ? `${primary ? 3 : 2}px ${primary ? "solid" : "dashed"} #162235`
                              : undefined,
                            outlineOffset: -3,
                            boxShadow: ecmCell
                              ? "inset 0 0 0 4px #7c3aed, inset 0 0 0 6px #f8fafc"
                              : undefined,
                            fontWeight: primary || ecmCell ? 800 : 500,
                          }}
                        >
                          {primary && (
                            <span aria-hidden="true" className="absolute left-1 top-0 text-[10px]">
                              ●
                            </span>
                          )}
                          {ecmCell && (
                            <span aria-hidden="true" className="absolute right-1 top-0 text-[10px]">
                              ECM
                            </span>
                          )}
                          {value.toFixed(2)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {coverage ? (
            <div className="flex flex-wrap items-center gap-3 text-xs text-fg-muted">
              <span className="inline-flex items-center gap-1">
                <span
                  aria-hidden="true"
                  className="inline-block h-3 w-5 rounded"
                  style={{
                    backgroundColor: coverageColor(0, maxCount),
                    backgroundImage:
                      "repeating-linear-gradient(45deg, transparent 0 3px, rgba(100,116,139,0.35) 3px 4px)",
                  }}
                />
                never visited
              </span>
              <span className="inline-flex items-center gap-1">
                <span
                  aria-hidden="true"
                  className="inline-block h-3 w-5 rounded"
                  style={{ backgroundColor: coverageColor(1, maxCount) }}
                />
                thin (&lt; {THIN_COVERAGE_SAMPLES} samples)
              </span>
              <span className="inline-flex items-center gap-1">
                <span
                  aria-hidden="true"
                  className="inline-block h-3 w-5 rounded"
                  style={{
                    backgroundColor: coverageColor(
                      Math.max(maxCount, THIN_COVERAGE_SAMPLES),
                      maxCount,
                    ),
                  }}
                />
                visited (up to {maxCount})
              </span>
              <span>Shading is sample count; hover a cell for its count.</span>
            </div>
          ) : (
            <div className="flex items-center gap-3 text-xs text-fg-muted">
              <span>{min.toFixed(2)}</span>
              <span
                aria-hidden="true"
                className="h-2 w-40 rounded"
                style={{
                  background: `linear-gradient(to right, ${mapColor(min, min, max)}, ${mapColor(min + (max - min) / 3, min, max)}, ${mapColor(min + (2 * (max - min)) / 3, min, max)}, ${mapColor(max, min, max)})`,
                }}
              />
              <span>
                {max.toFixed(2)} {table.unit}
              </span>
              <span>Low → high; not a safety rating</span>
            </div>
          )}
        </>
      )}
    </div>
  );
}

import { useCallback, useMemo, useState, type ReactNode } from "react";

import { getRomTrace } from "../../../api/workbench";
import { Notice } from "../../../components/Notice";
import { useControlResource } from "../../../hooks/useControlResource";
import { useLiveOverlay, type TableOverlay } from "../../../hooks/useLiveOverlay";
import type { CoverageSource } from "../../../hooks/useCoverageOverlay";
import { useTelemetryChannels } from "../../../hooks/telemetryChannels";
import { overlayTableOf, type OverlayTable } from "../../../lib/romOverlay";
import type { RomTable } from "../../../lib/romMaps";
import { describeDisagreement } from "../../../lib/romTrace";
import { buttonClass, inputClass } from "../../../styles";
import type { StatusResponse, WorkbenchItem } from "../../../types";
import type { RomTableValues } from "../../../types/romLibrary";
import type { RomTraceContext } from "../../../types/workbench";
import { type GridOverlay } from "../../cellOverlay";
import { CellGrid } from "./CellGrid";

interface Props {
  token: string;
  sha: string;
  status: StatusResponse | null;
  /** The log picked on Logs (W02.5): the coverage overlay's subject. */
  selectedLogId: string | null;
  onNavigate?: (item: WorkbenchItem) => void;
  /** The table being viewed, and every table of the image. */
  table: RomTableValues;
  tables: RomTableValues[];
  /** The grid, handed the overlay's marks (null where none applies). */
  children: (overlay: GridOverlay | null) => ReactNode;
}

/** The editor's live overlay (ROM Maps, merged into the editor 2026-10-26):
 *  the inferred operating-point cell, the ECM's own cell and log coverage on
 *  the tables the vehicle maps a trace to.
 *
 *  Read-only and gated: nothing is called an active cell until the operator
 *  asserts this ROM and slot are running and nothing the daemon knows
 *  contradicts it (`traceGate`), and each refusal says why. The inputs come
 *  from `GET .../trace` (the vehicle's `rom_viewer.yaml`); a vehicle without
 *  one has no overlay, and the editor works as before. */
export function LiveOverlay(props: Props) {
  const { token, sha, children } = props;
  const trace = useControlResource(
    useCallback((signal: AbortSignal) => getRomTrace(token, sha, signal), [token, sha]),
    { enabled: Boolean(token), intervalMs: null },
  );
  if (trace.loading || trace.stale)
    return (
      <>
        <p role="status" className="text-sm text-fg-dim">
          Reading the live overlay&apos;s inputs…
        </p>
        {children(null)}
      </>
    );
  if (trace.error || !trace.data)
    return (
      <>
        <Notice
          notice={{ kind: "error", text: `Live overlay unavailable. ${trace.error ?? ""}` }}
        />
        {children(null)}
      </>
    );
  return <Overlay {...props} context={trace.data} />;
}

/** Why the live overlay has nothing to place on `table`. */
function noOverlayNote(
  table: RomTableValues,
  context: RomTraceContext,
  placed: OverlayTable | null,
): string {
  if (placed && "refusal" in placed) return placed.refusal;
  const info = context.tables.find((item) => item.kind === table.kind);
  if (table.slot === context.map_slot && info && info.trace === null)
    return `This vehicle's rom_viewer.yaml maps no trace channels to ${table.name}; no ECM cell is drawn.`;
  return `The live overlay places cells on the tables the vehicle maps a trace to, in Map ${context.map_slot}. ${table.name}${table.slot === null ? "" : ` in Map ${table.slot}`} has none.`;
}

function Overlay({ context, ...props }: Props & { context: RomTraceContext }) {
  const { token, status, selectedLogId, onNavigate, table, tables, children } = props;
  const channels = useTelemetryChannels();
  const [drive, setDrive] = useState(false);
  // Each table of the image as the trace code reads it (null: the overlay does
  // not apply to it). Memoised so the coverage hook's effects see a stable list.
  const placed = useMemo(
    () => tables.map((source) => ({ source, item: overlayTableOf(source, context) })),
    [tables, context],
  );
  const usable = useMemo(
    () =>
      placed.flatMap(({ source, item }) =>
        item && "table" in item ? [{ source, table: item.table }] : [],
      ),
    [placed],
  );
  const live = useLiveOverlay({
    token,
    channels,
    status,
    selectedLogId,
    context,
    tables: useMemo(() => usable.map((entry) => entry.table), [usable]),
  });
  const mine = placed.find(({ source }) => source === table)?.item ?? null;
  const overlayTable = mine && "table" in mine ? mine.table : null;
  const shown = overlayTable ? live.forTable(overlayTable) : null;
  const driveTarget = usable[0] ?? null;
  const calibration = status?.calibration_matches_registry;

  return (
    <div className="space-y-3" aria-label="Live overlay">
      <div className="space-y-1 rounded-lg border border-border p-3 text-sm">
        <p>
          Recorded running revision: {context.recorded_revision ?? "unknown"}.{" "}
          {context.matches_recorded_revision
            ? "This image's hash matches that record; this is not ECU readback."
            : "This image's hash does not match — historical viewing only."}
        </p>
        {calibration === true && (
          <p>
            ECU calibration id {status?.ecu_calibration_id} matches the flash history&apos;s running
            revision.
          </p>
        )}
        {(calibration === null || calibration === undefined) && (
          <p className="text-fg-dim">
            ECU calibration id not read yet; the slot assertion below is the only check that this
            ROM is running.
          </p>
        )}
        <div className="flex flex-wrap items-center gap-3 pt-1">
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={live.confirmed}
              disabled={!context.matches_recorded_revision}
              onChange={(event) => live.setConfirmed(event.target.checked)}
            />
            I confirm this ROM and Map {context.map_slot} are currently active in the ECU.
          </label>
          {driveTarget && (
            <button className={buttonClass} onClick={() => setDrive(true)}>
              Drive presentation
            </button>
          )}
        </div>
      </div>
      <div className="flex flex-wrap gap-5 font-mono text-sm" aria-label="Live map inputs">
        <span>RPM: {live.rpm ? live.rpm.value.toFixed(0) : "unavailable"}</span>
        <span>BFS: {live.bfs ? `${live.bfs.value.toFixed(2)} ms` : "unavailable"}</span>
        <span>
          Final commanded AFR:{" "}
          {live.commanded ? live.commanded.value.toFixed(2) : "unavailable / fuel cut"}
        </span>
      </div>
      <p role="status" className="text-sm text-fg-dim">
        {live.traceReason ||
          "Live inferred RPM/BFS lookup. Solid outline = nearest breakpoint; dashed outlines = surrounding cells. ECU interpolation/overrides are not verified."}
      </p>
      {overlayTable && shown ? (
        <TableNotes
          table={overlayTable}
          shown={shown}
          live={live}
          selectedLogId={selectedLogId}
          onNavigate={onNavigate}
        />
      ) : (
        <p role="note" className="text-sm text-fg-dim">
          {noOverlayNote(table, context, mine)}
        </p>
      )}
      {children(shown?.grid ?? null)}
      <ul className="list-disc space-y-1 pl-5 text-xs text-fg-dim">
        {context.warnings.map((warning) => (
          <li key={warning}>{warning}</li>
        ))}
      </ul>
      {drive && driveTarget && (
        <div
          role="dialog"
          aria-label="Drive presentation"
          className="fixed inset-0 z-50 space-y-3 overflow-auto bg-bg p-4 text-lg"
        >
          <div className="flex items-center gap-3">
            <h3 className="mr-auto text-2xl font-semibold">
              Map {driveTarget.table.slot} · {driveTarget.source.name}
            </h3>
            <button className={buttonClass} onClick={() => setDrive(false)}>
              Exit drive presentation
            </button>
          </div>
          <p role="status" className="text-fg-dim">
            {live.traceReason || "Inferred lookup live."}
          </p>
          <CellGrid
            table={driveTarget.source}
            label={`${driveTarget.source.label ?? driveTarget.source.name} drive presentation`}
            overlay={live.forTable(driveTarget.table).grid}
            large
          />
        </div>
      )}
    </div>
  );
}

function TableNotes({
  table,
  shown,
  live,
  selectedLogId,
  onNavigate,
}: {
  table: RomTable;
  shown: TableOverlay;
  live: ReturnType<typeof useLiveOverlay>;
  selectedLogId: string | null;
  onNavigate?: (item: WorkbenchItem) => void;
}) {
  const now = live.now;
  const { coverage } = live;
  const view = coverage.view(table);
  const source = coverage.sourceFor(table);
  const state = shown.ecm;
  const name = `Map ${table.slot} ${table.title}`;
  return (
    <div className="space-y-2">
      {table.trace && state && (
        <div className="space-y-1 text-sm" aria-label={`ECM trace ${name}`}>
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
              ? `ECM cell: double ring — the cell the ECM's own trace coordinates point at.${shown.clampNote ? ` ${shown.clampNote}` : ""}`
              : state.reason}
          </p>
          {shown.disagreement && (
            <p role="alert" className="text-warn">
              {describeDisagreement(shown.disagreement, table)} A persistent disagreement is a
              finding: a wrong axis decode, a wrong BFS scale, or interpolation the inference does
              not model.
            </p>
          )}
          {state.position && shown.inferred && !shown.disagreement && (
            <p>ECM cell and inferred cell agree.</p>
          )}
          {shown.tally && shown.tally.checks > 0 && (
            <p className="text-xs text-fg-dim">
              Disagreed on {shown.tally.disagreements} of {shown.tally.checks} ECM samples since
              tracing began
              {shown.tally.streakSince !== null
                ? `; current disagreement for ${((now - shown.tally.streakSince) / 1000).toFixed(0)} s`
                : ""}
              .
            </p>
          )}
        </div>
      )}
      {shown.outOfRange && (
        <p role="status">Operating point is outside the verified axes; no cell highlighted.</p>
      )}
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            aria-label={`Coverage overlay ${name}`}
            checked={coverage.isOn(table)}
            onChange={(event) => coverage.toggle(table, event.target.checked)}
          />
          Coverage overlay
        </label>
        {coverage.isOn(table) && (
          <select
            aria-label={`Coverage source ${name}`}
            className={inputClass}
            value={source}
            onChange={(event) => coverage.choose(table, event.target.value as CoverageSource)}
          >
            <option value="log">Selected log{selectedLogId ? ` (${selectedLogId})` : ""}</option>
            <option value="live">This capture (live)</option>
          </select>
        )}
      </div>
      {view && (
        <p role="status" className="text-sm text-fg-dim">
          {view.note}
          {view.action === "logs" && onNavigate && (
            <>
              {" "}
              <button className={buttonClass} onClick={() => onNavigate("tuning-logs")}>
                Open Logs
              </button>
            </>
          )}
        </p>
      )}
    </div>
  );
}

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  getRomLibrary,
  getRomTables,
  previewRomEdits,
  simulateRomEdits,
  uploadRomImage,
} from "../../api/workbench";
import { Notice } from "../../components/Notice";
import { Panel } from "../../components/Panel";
import { SegmentedControl, type SegmentOption } from "../../components/SegmentedControl";
import { describeError, useControlResource } from "../../hooks/useControlResource";
import { useIsNarrow } from "../../hooks/useMediaQuery";
import type { ChannelSnapshot } from "../../hooks/useTelemetryStream";
import type { LoadedRom } from "../../hooks/useTuningContext";
import {
  SIM_SLOT,
  SIMULATE_REASON_TEXT,
  buildEdits,
  describeOperation,
  duplicateCells,
  isEditableTable,
  isViewableTable,
  loadEditorState,
  pendingCellSet,
  previewRefusal,
  saveEditorState,
  simulateReason,
  specFileName,
  tableChoice,
  valueMask,
} from "../../lib/romEditor";
import {
  ROM_UPLOAD_LIMIT,
  axisBreakpoints,
  describeEditorDisagreement,
  entryTitle,
  refusalText,
  tableSlots,
} from "../../lib/romLibrary";
import {
  SIM_BLOCK_TEXT,
  gridPoints,
  resultFileName,
  simFailure,
  suggestEdges,
} from "../../lib/romSim";
import { editsTouchWotColumn } from "../../lib/romSim";
import { expectedRun, fineReference, loadTimings, recordTiming } from "../../lib/romSimSize";
import type { StatusResponse, WorkbenchItem } from "../../types";
import type {
  CellSelection,
  PendingEdit,
  RomEditOperation,
  RomEditPreview,
  RomEditRefusal,
  RomSimRequest,
  RomSimResult,
  SimFailure,
  SimRunContext,
} from "../../types/romEditor";
import type { RomLibraryEntry, RomTableValues } from "../../types/romLibrary";
import { downloadText } from "../../util/browser";
import { BasePicker, SimulateChip } from "./romEditor/BasePicker";
import type { GridOverlay } from "../cellOverlay";
import { CellGrid } from "./romEditor/CellGrid";
import { LiveOverlay } from "./romEditor/LiveOverlay";
import { OpenRom } from "./romEditor/OpenRom";
import { OperationPanel } from "./romEditor/OperationPanel";
import { PendingEdits } from "./romEditor/PendingEdits";
import { PreviewResult } from "./romEditor/PreviewResult";
import { RecentMenu } from "./romEditor/RecentMenu";
import { SimCancelled, SimFailureAlert, SimForm, SimRunning } from "./romEditor/SimForm";
import { SimResults } from "./romEditor/SimResults";
import { TableTree } from "./romEditor/TableTree";
import { RevisionChip } from "./romLibraryParts";

/** The chosen image's sha lives in `?rom=<sha>`. The hash is the router's
 *  (`#wb/<page>`), which rewrites it, so the sha rides in the query. */
const URL_PARAM = "rom";

function shaFromUrl(): string | null {
  try {
    return new URLSearchParams(window.location.search).get(URL_PARAM);
  } catch {
    return null;
  }
}

function writeShaToUrl(sha: string) {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set(URL_PARAM, sha);
    window.history.replaceState(null, "", url);
  } catch {
    // A sandboxed frame: the choice just is not bookmarkable.
  }
}

/** The Workspace's view rides beside it in `?view=` (W10.9); a missing or
 *  unknown value is Tables. */
const VIEW_PARAM = "view";

type RomView = "tables" | "edits" | "simulate";

const ROM_VIEWS: readonly RomView[] = ["tables", "edits", "simulate"];

function viewFromUrl(): RomView {
  try {
    const value = new URLSearchParams(window.location.search).get(VIEW_PARAM);
    return ROM_VIEWS.find((view) => view === value) ?? "tables";
  } catch {
    return "tables";
  }
}

function writeViewToUrl(view: RomView) {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set(VIEW_PARAM, view);
    window.history.replaceState(null, "", url);
  } catch {
    // As for the sha.
  }
}

/** No live telemetry: the editor works without it (the overlay then has no
 *  inputs, and says so). */
const NO_CHANNELS = new Map<string, ChannelSnapshot>();

/** Engine Tuning ▸ ROM Editor (W09.3), and since 2026-10-26 the one page for
 *  viewing ROM tables too (it absorbed ROM Maps): pick a stored image, look at
 *  every table its map declares but the axes, shaded low to high, edit the
 *  fuel chain and timing trims in Map 1, preview the result, download the edit
 *  spec.
 *
 *  Loaded, the image is a one-line header (name, sha, chips, Open another
 *  ROM…, Recent ▾) over three views (W10): Tables (a tree grouped as
 *  `rom_map.yaml` files the tables, a select below 900px, beside the chosen
 *  table's grid and operation), Edits & Preview, and Simulate. The view rides
 *  in `?view=` beside `?rom=`. Hidden views stay mounted, so a half-typed
 *  operation or Simulate form survives a switch, and a run keeps going when
 *  its view is left. The live overlay (inferred cell, ECM cell, log coverage)
 *  sits behind a toggle on the tables the vehicle maps a trace to, mounted
 *  only while Tables shows. The edited image never leaves the server: it is
 *  not stored, not downloadable, and not flashable. A flash is the Flash
 *  page's explicit act. */
export function RomEditorPage({
  token,
  channels = NO_CHANNELS,
  status,
  selectedLogId = null,
  onNavigate,
  onLoadedChange,
}: {
  token: string;
  status: StatusResponse | null;
  /** Live telemetry, for the overlay's inferred and ECM cells. */
  channels?: Map<string, ChannelSnapshot>;
  /** The log picked on Logs: the coverage overlay's subject. */
  selectedLogId?: string | null;
  onNavigate?: (item: WorkbenchItem) => void;
  /** The Engine Tuning group's record of the image in view (its context bar). */
  onLoadedChange?: (rom: LoadedRom | null) => void;
}) {
  const enabled = Boolean(token);
  const library = useControlResource(
    useCallback((signal: AbortSignal) => getRomLibrary(token, signal), [token]),
    { enabled, intervalMs: null },
  );
  const stored = library.data?.entries ?? [];
  // A file just opened is held here until the library list has caught up, so
  // the editor never shows another image in between.
  const [opened, setOpened] = useState<OpenedRom | null>(null);
  const entries =
    opened && !stored.some((entry) => entry.sha256 === opened.entry.sha256)
      ? [opened.entry, ...stored]
      : stored;
  const [chosen, setChosen] = useState<string | null>(shaFromUrl);
  // Unloaded until the operator opens a file, picks a recent one, or arrives
  // by a `?rom=<sha>` link: never a base nobody chose.
  const selected = entries.find((entry) => entry.sha256 === chosen) ?? null;
  const selectedSha = selected?.sha256 ?? null;
  useEffect(() => {
    if (selectedSha) writeShaToUrl(selectedSha);
  }, [selectedSha]);

  const [busy, setBusy] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const load = useRef(0);

  /** Load a file into the editor in one step: upload it (the library keeps it
   *  by sha256, idempotently), make the answered entry the base. A refusal
   *  leaves the current base as it was. */
  async function openFile(file: File | undefined) {
    if (!file || !enabled) return;
    setRefusal(null);
    if (!file.size) {
      setRefusal(`${file.name} is empty.`);
      return;
    }
    if (file.size > ROM_UPLOAD_LIMIT) {
      setRefusal(`${file.name} is larger than the 2 MiB upload limit.`);
      return;
    }
    const mine = load.current + 1;
    load.current = mine;
    setBusy(`Opening ${file.name}`);
    try {
      const entry = await uploadRomImage(token, file);
      if (load.current !== mine) return;
      setOpened({ entry, name: file.name, existed: entry.created === false });
      setChosen(entry.sha256);
      writeShaToUrl(entry.sha256);
      library.refresh();
    } catch (err) {
      if (load.current === mine) setRefusal(`${file.name}: ${refusalText(err)}`);
    } finally {
      if (load.current === mine) setBusy(null);
    }
  }

  const loading = enabled && library.loading;
  const recent = [...entries].sort((a, b) => Date.parse(b.added_at) - Date.parse(a.added_at));
  const linkMissing =
    enabled &&
    !!library.data &&
    !!chosen &&
    !selected &&
    !library.loading &&
    !library.error &&
    !library.stale;
  const nothingLoaded = !selected;
  const name =
    selected && opened?.entry.sha256 === selected.sha256
      ? opened.name
      : selected && entryTitle(selected);
  useEffect(() => {
    onLoadedChange?.(selectedSha && name ? { sha256: selectedSha, name } : null);
  }, [onLoadedChange, selectedSha, name]);
  const choose = (sha: string) => {
    setChosen(sha);
    writeShaToUrl(sha);
  };
  return (
    <section className="space-y-4">
      {/* Not clipped: the Recent ▾ menu hangs below this one-line panel. */}
      <Panel
        title="ROM Editor"
        count={library.data ? `${entries.length} ${entries.length === 1 ? "image" : "images"}` : ""}
        className="overflow-visible!"
      >
        <div className={nothingLoaded ? "space-y-4" : "space-y-2"}>
          {nothingLoaded && (
            <p className="text-sm text-fg-dim">
              Look at every table in a ROM, shaded from low to high, and edit the fuel chain (Fuel
              Targets, Fuel Compensation and K) and the ignition timing trims (A and B) in Map 1,
              then see the tables after the edits. The edited image is never stored or offered: you
              get an edit spec, a reviewable file that reproduces the edit from its base. Opening a
              file adds it to the ROM library, kept by its hash. Nothing here writes the ECU.
            </p>
          )}
          {!token && <p role="status">Enter the control token to use the ROM Editor.</p>}
          {library.error && <Notice notice={{ kind: "error", text: library.error }} />}
          <OpenRom
            label={nothingLoaded ? "Open ROM…" : "Open another ROM…"}
            primary={nothingLoaded && !loading}
            disabled={!enabled || !!busy}
            dragging={dragging}
            onDraggingChange={setDragging}
            onFile={(file) => void openFile(file)}
            bare={!nothingLoaded}
            trailing={
              !nothingLoaded && (
                <RecentMenu entries={recent} selectedSha={selectedSha} onChoose={choose} />
              )
            }
          >
            {selected ? (
              <div className="flex min-w-0 flex-wrap items-center gap-2" aria-label="Loaded ROM">
                <span className="font-semibold">{name}</span>
                <span className="font-num text-xs text-fg-faint">
                  {selected.sha256.slice(0, 12)}
                </span>
                <RevisionChip match={selected.revision_match} />
                <SimulateChip reason={simulateReason(selected.revision_match)} />
              </div>
            ) : (
              <span className="text-sm text-fg-dim">
                Choose a ROM file, or drop one here: a plain image (.bin) or an .OsirisROM.
              </span>
            )}
          </OpenRom>
          {busy && <Notice notice={{ kind: "busy", text: `${busy}…` }} />}
          {refusal && <Notice notice={{ kind: "error", text: refusal }} />}
          {opened?.existed && opened.entry.sha256 === selectedSha && !busy && (
            <p role="status" className="text-xs text-fg-dim">
              {opened.name} is already in the library; nothing new was stored.
            </p>
          )}
          {linkMissing && (
            <p role="status" className="text-xs text-fg-dim">
              The ROM this link names ({chosen?.slice(0, 12)}) is not in the library. Open a file or
              choose a recent ROM.
            </p>
          )}
          {loading && <p className="text-fg-dim">Reading the library…</p>}
          {nothingLoaded && recent.length > 0 && (
            <div className="space-y-2">
              <h3 className="text-sm font-semibold">Recent ROMs</h3>
              <BasePicker entries={recent} selectedSha={selectedSha} onChoose={choose} />
            </div>
          )}
        </div>
      </Panel>
      {selected && (
        <Workspace
          key={selected.sha256}
          token={token}
          entry={selected}
          channels={channels}
          status={status}
          selectedLogId={selectedLogId}
          onNavigate={onNavigate}
        />
      )}
    </section>
  );
}

interface OpenedRom {
  entry: RomLibraryEntry;
  /** The file's own name, kept client-side for the header. */
  name: string;
  existed: boolean;
}

type PreviewState =
  | { kind: "idle" }
  | { kind: "busy" }
  | { kind: "ok"; data: RomEditPreview }
  | { kind: "refused"; refusal: RomEditRefusal }
  | { kind: "error"; text: string };

/** What a run in flight is expected to take (the Cancel wording). */
interface Expected {
  seconds: number;
  measured: boolean;
}

type SimState =
  | { kind: "idle" }
  | { kind: "running"; startedAt: number; expected: Expected }
  /** The page stopped waiting; the daemon did not stop. */
  | { kind: "cancelled"; startedAt: number; cancelledAt: number; expected: Expected }
  | { kind: "ok"; result: RomSimResult; run: SimRunContext }
  | { kind: "failed"; failure: SimFailure };

function Workspace({
  token,
  entry,
  channels,
  status,
  selectedLogId,
  onNavigate,
}: {
  token: string;
  entry: RomLibraryEntry;
  channels: Map<string, ChannelSnapshot>;
  status: StatusResponse | null;
  selectedLogId: string | null;
  onNavigate?: (item: WorkbenchItem) => void;
}) {
  const sha = entry.sha256;
  const tables = useControlResource(
    useCallback((signal: AbortSignal) => getRomTables(token, sha, signal), [token, sha]),
    { enabled: Boolean(token), intervalMs: null },
  );
  // Every table the map declares that is worth a grid; Map 1 until another
  // slot is chosen (it is the one the editor edits and the simulator runs).
  const viewable = useMemo(
    () => (tables.data?.tables ?? []).filter(isViewableTable),
    [tables.data],
  );
  const slots = tableSlots(viewable);
  const [slotChoice, setSlotChoice] = useState<number | null>(null);
  const slot =
    slotChoice !== null && slots.includes(slotChoice)
      ? slotChoice
      : slots.includes(SIM_SLOT)
        ? SIM_SLOT
        : (slots[0] ?? null);
  const inSlot = viewable.filter((item) => item.slot === null || item.slot === slot);
  const [choice, setChoice] = useState<string | null>(null);
  const table = inSlot.find((item) => tableChoice(item) === choice) ?? inSlot[0] ?? null;
  const editable = table !== null && isEditableTable(table);
  const [overlayOn, setOverlayOn] = useState(false);
  const [view, setView] = useState<RomView>(viewFromUrl);
  const narrow = useIsNarrow();

  const [selection, setSelection] = useState<CellSelection | null>(null);
  const [saved] = useState(() => loadEditorState(sha));
  const [pending, setPending] = useState<PendingEdit[]>(saved.pending);
  const [label, setLabel] = useState(saved.label);
  const [opRefusal, setOpRefusal] = useState<{ reason: string; message: string } | null>(null);
  const [preview, setPreview] = useState<PreviewState>({ kind: "idle" });
  const request = useRef(0);
  const counter = useRef(0);
  const baseTables = tables.data?.tables ?? [];
  const [sim, setSim] = useState<SimState>({ kind: "idle" });
  // This server's earlier runs, kept by the browser: the cost estimate's basis.
  const [timings, setTimings] = useState(loadTimings);
  const simRun = useRef(0);
  const simAbort = useRef<AbortController | null>(null);
  // A run this page stopped waiting for, so a later `analysis_busy` can be
  // explained as that run. Cleared by the next run that returns.
  const [abandoned, setAbandoned] = useState<{ startedAt: number; expectedS: number } | null>(null);
  const edits = useMemo(() => pending.map((item) => item.edit), [pending]);

  // Leaving the image (or the page) drops the wait; the daemon's job is its own.
  useEffect(
    () => () => {
      simRun.current += 1;
      simAbort.current?.abort();
    },
    [],
  );

  useEffect(() => {
    saveEditorState(sha, { label, pending });
  }, [sha, label, pending]);

  function invalidate() {
    request.current += 1;
    setPreview({ kind: "idle" });
  }

  function apply(op: RomEditOperation) {
    if (!table) return;
    if (!selection) {
      setOpRefusal({ reason: "no_selection", message: "select the cells to edit first" });
      return;
    }
    const built = buildEdits(table, selection, op);
    if (!built.ok) {
      setOpRefusal({ reason: built.reason, message: built.message });
      return;
    }
    const [edit] = built.edits;
    if (!edit) return;
    const duplicates = duplicateCells(pending, edit);
    if (duplicates.length > 0) {
      setOpRefusal({
        reason: "duplicate_edit",
        message: `${duplicates.length} selected ${duplicates.length === 1 ? "cell is" : "cells are"} already in a pending edit; remove that edit first (composition order is not expressible)`,
      });
      return;
    }
    setOpRefusal(null);
    counter.current += 1;
    setPending((items) => [
      ...items,
      {
        id: `${Date.now()}-${counter.current}`,
        summary: describeOperation(op, built.cells.length, table.decode?.unit ?? null),
        edit,
        tableName: table.name,
        chain: built.chain,
      },
    ]);
    invalidate();
  }

  async function runPreview() {
    const mine = request.current + 1;
    request.current = mine;
    setPreview({ kind: "busy" });
    try {
      const data = await previewRomEdits(token, sha, {
        label: label.trim(),
        edits: pending.map((item) => item.edit),
      });
      if (request.current === mine) setPreview({ kind: "ok", data });
    } catch (err) {
      if (request.current !== mine) return;
      const refusal = previewRefusal(err);
      setPreview(
        refusal ? { kind: "refused", refusal } : { kind: "error", text: describeError(err) },
      );
    }
  }

  async function runSimulation(simRequest: RomSimRequest) {
    const mine = simRun.current + 1;
    simRun.current = mine;
    simAbort.current?.abort();
    const controller = new AbortController();
    simAbort.current = controller;
    const startedAt = Date.now();
    const points = gridPoints(simRequest.rpm_edges, simRequest.load_edges);
    const expected = expectedRun(points, simRequest.draws, timings);
    setSim({ kind: "running", startedAt, expected });
    try {
      const result = await simulateRomEdits(token, sha, simRequest, controller.signal);
      if (simRun.current !== mine) return;
      // This server's own wall time for this size: the next estimate's basis.
      recordTiming({ points, draws: simRequest.draws, seconds: (Date.now() - startedAt) / 1000 });
      setTimings(loadTimings());
      setAbandoned(null);
      setSim({
        kind: "ok",
        result,
        run: {
          request: simRequest,
          edits: pending.map((item) => ({
            tableName: item.tableName,
            summary: item.summary,
            chain: item.chain,
          })),
          base: {
            sha256: sha,
            label: entry.label ?? entry.filename ?? sha.slice(0, 12),
            revision: entry.revision_match.revision,
          },
          wotColumnEdited: editsTouchWotColumn(simRequest.edits, baseTables),
          elapsedS: Math.round((Date.now() - startedAt) / 1000),
          fineReference: fineReference(suggestEdges(baseTables)),
        },
      });
    } catch (err) {
      if (simRun.current !== mine) return;
      setSim({ kind: "failed", failure: simFailure(err) });
    }
  }

  function cancelSimulation() {
    if (sim.kind !== "running") return;
    // Drop the wait (the result, if it ever comes, is not shown); the daemon's
    // job is its own and no route stops it.
    simRun.current += 1;
    simAbort.current?.abort();
    setAbandoned({ startedAt: sim.startedAt, expectedS: sim.expected.seconds });
    setSim({
      kind: "cancelled",
      startedAt: sim.startedAt,
      cancelledAt: Date.now(),
      expected: sim.expected,
    });
  }

  if (tables.loading || tables.stale) return <p className="text-fg-dim">Reading tables…</p>;
  if (tables.error) return <Notice notice={{ kind: "error", text: tables.error }} />;
  if (!table) {
    return (
      <Panel title="Tables">
        <p className="text-fg-dim">
          {tables.data?.reason ?? "This image has no table to show or edit."}
        </p>
      </Panel>
    );
  }

  const decodeLine = [
    table.dtype,
    table.decoded ? (table.decode?.formula ?? null) : "raw: decode unconfirmed",
    table.confidence,
  ]
    .filter(Boolean)
    .join(" · ");
  const rowAxis = table.axes.rows ? axisBreakpoints(table.axes.rows) : null;
  const colAxis = table.axes.cols ? axisBreakpoints(table.axes.cols) : null;
  const mask = valueMask(table.decode);
  const maskBits = mask === null ? null : `the low ${mask.toString(2).length} bits`;
  const simBlockedReason =
    preview.kind === "refused"
      ? "preview_refused"
      : preview.kind === "ok"
        ? preview.data.sim_base.reason
        : simulateReason(entry.revision_match);
  const simBlocked = simBlockedReason
    ? {
        reason: simBlockedReason,
        text: SIM_BLOCK_TEXT[simBlockedReason] ?? SIMULATE_REASON_TEXT[simBlockedReason] ?? "",
      }
    : null;

  const simStale =
    sim.kind === "ok" && JSON.stringify(sim.run.request.edits) !== JSON.stringify(edits);
  const simState =
    sim.kind === "running"
      ? " · running"
      : sim.kind === "failed"
        ? " · failed"
        : simStale
          ? " · stale"
          : "";
  const views: ReadonlyArray<SegmentOption<RomView>> = [
    { id: "tables", label: "Tables" },
    {
      id: "edits",
      label: pending.length > 0 ? `Edits & Preview (${pending.length})` : "Edits & Preview",
    },
    { id: "simulate", label: `Simulate${simState}` },
  ];
  const chooseTable = (item: RomTableValues) => {
    setChoice(tableChoice(item));
    setSelection(null);
    setOpRefusal(null);
  };

  const grid = (overlay: GridOverlay | null) => (
    <CellGrid
      key={`grid-${tableChoice(table)}`}
      table={table}
      label={`${table.label ?? table.name} cells`}
      selection={editable ? selection : null}
      onSelect={editable ? setSelection : undefined}
      pendingCells={pendingCellSet(pending, table.id ?? "")}
      overlay={overlay}
    />
  );

  return (
    <div className="space-y-4">
      <SegmentedControl
        label="ROM Editor views"
        options={views}
        value={view}
        onChange={(next) => {
          setView(next);
          writeViewToUrl(next);
        }}
        className="flex-wrap"
      />
      {/* Every view stays mounted while hidden: SimForm's typed fields and
          OperationPanel's value would not survive an unmount. */}
      <div hidden={view !== "tables"}>
        <Panel title="Tables" count={`${inSlot.length}`}>
          <div className="grid grid-cols-[15rem_minmax(0,1fr)] items-start gap-4 max-[900px]:grid-cols-1">
            <TableTree
              tables={inSlot}
              chosen={table}
              slots={slots}
              slot={slot}
              pending={pending}
              narrow={narrow}
              onSlot={(number) => {
                setSlotChoice(number);
                setChoice(null);
                setSelection(null);
                setOpRefusal(null);
              }}
              onChoose={chooseTable}
            />
            <div className="min-w-0 space-y-4">
              <h3 className="text-base font-semibold">{table.label ?? table.name}</h3>
              <p className="font-num text-xs text-fg-faint">{decodeLine}</p>
              <p className="text-xs text-fg-dim">
                {rowAxis
                  ? `Rows: ${rowAxis.label}${rowAxis.unit ? ` (${rowAxis.unit})` : ""}`
                  : "Rows: index"}
                {" · "}
                {colAxis
                  ? `Columns: ${colAxis.label}${colAxis.unit ? ` (${colAxis.unit})` : ""}`
                  : "Columns: index"}
                {" · "}Cells: {table.decoded ? (table.unit ?? "value") : "raw"}
              </p>
              {table.editor_disagreement && (
                <p
                  role="note"
                  aria-label={`${table.name} editor labels`}
                  className="text-sm text-warn"
                >
                  {describeEditorDisagreement(table.editor_disagreement)}
                </p>
              )}
              {editable ? (
                <p className="text-xs text-fg-faint">
                  Click a cell, shift-click or drag for a range; arrows move, shift+arrows extend.
                  Dashed cells are in a pending edit.
                </p>
              ) : (
                <p role="note" className="text-xs text-fg-dim">
                  Viewing only:{" "}
                  {table.slot !== null && table.slot !== SIM_SLOT
                    ? `edits and the simulator cover Map ${SIM_SLOT} only.`
                    : `${table.name} is not one of the tables edited here.`}
                </p>
              )}
              {maskBits !== null && (
                <p role="note" className="text-xs text-fg-dim">
                  The ECM reads {maskBits} of each cell and strips the rest as a flag (decode mask{" "}
                  <span className="font-num">0x{mask?.toString(16).toUpperCase()}</span>). The grid
                  shows the value; the raw count, flag included, is on hover.
                  {editable &&
                    " Add and Scale act on the value and keep each cell's flag; Set to value keeps it too. A raw count may not change it."}
                </p>
              )}
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={overlayOn}
                  onChange={(event) => setOverlayOn(event.target.checked)}
                />
                Live overlay: inferred cell, ECM cell and log coverage
              </label>
              {overlayOn && view === "tables" ? (
                <LiveOverlay
                  token={token}
                  sha={sha}
                  channels={channels}
                  status={status}
                  selectedLogId={selectedLogId}
                  onNavigate={onNavigate}
                  table={table}
                  tables={viewable}
                >
                  {(overlay) => grid(overlay)}
                </LiveOverlay>
              ) : (
                grid(null)
              )}
              {editable && (
                <OperationPanel
                  key={`operation-${table.id}`}
                  table={table}
                  selection={selection}
                  refusal={opRefusal}
                  onApply={apply}
                />
              )}
            </div>
          </div>
        </Panel>
      </div>

      <div hidden={view !== "edits"} className="space-y-4">
        <Panel title="Pending edits" count={`${pending.length}`}>
          <PendingEdits
            pending={pending}
            label={label}
            busy={preview.kind === "busy"}
            onLabel={(value) => {
              setLabel(value);
              invalidate();
            }}
            onRemove={(id) => {
              setPending((items) => items.filter((item) => item.id !== id));
              invalidate();
            }}
            onPreview={() => void runPreview()}
          />
        </Panel>

        <Panel title="Preview">
          <div className="space-y-3" aria-live="polite">
            {preview.kind === "idle" && (
              <p className="text-sm text-fg-dim">
                Preview applies the pending edits to the image in memory and shows the tables after
                them.
              </p>
            )}
            {preview.kind === "busy" && <Notice notice={{ kind: "busy", text: "Previewing…" }} />}
            {preview.kind === "error" && <Notice notice={{ kind: "error", text: preview.text }} />}
            {preview.kind === "refused" && (
              <div role="alert" className="space-y-1 rounded-lg border border-bad/55 p-3">
                <p className="font-semibold text-bad">Refused: {preview.refusal.reason}</p>
                <p className="text-sm">{preview.refusal.message}</p>
              </div>
            )}
            {preview.kind === "ok" && (
              <PreviewResult
                preview={preview.data}
                baseTables={baseTables}
                onDownload={() =>
                  downloadText(preview.data.spec_yaml, specFileName(label), "application/x-yaml")
                }
              />
            )}
          </div>
        </Panel>
      </div>

      <section
        aria-label="Simulate"
        data-slot="simulate"
        hidden={view !== "simulate"}
        className="space-y-4"
      >
        <Panel title="Simulate">
          <div className="space-y-4">
            <SimForm
              token={token}
              tables={baseTables}
              label={label}
              edits={edits}
              blocked={simBlocked}
              startedAt={sim.kind === "running" ? sim.startedAt : null}
              timings={timings}
              onRun={(simRequest) => void runSimulation(simRequest)}
            />
            {sim.kind === "running" && (
              <SimRunning
                startedAt={sim.startedAt}
                expected={sim.expected}
                onCancel={cancelSimulation}
              />
            )}
            {sim.kind === "cancelled" && (
              <SimCancelled
                startedAt={sim.startedAt}
                cancelledAt={sim.cancelledAt}
                expected={sim.expected}
              />
            )}
            {sim.kind === "failed" && (
              <SimFailureAlert failure={sim.failure} cancelled={abandoned} />
            )}
          </div>
        </Panel>
        {sim.kind === "ok" && (
          <Panel title="Simulation result">
            <SimResults
              result={sim.result}
              run={sim.run}
              tables={baseTables}
              stale={simStale}
              onDownload={() =>
                downloadText(
                  JSON.stringify(sim.result),
                  resultFileName(sim.run.request.label),
                  "application/json",
                )
              }
            />
          </Panel>
        )}
      </section>
    </div>
  );
}

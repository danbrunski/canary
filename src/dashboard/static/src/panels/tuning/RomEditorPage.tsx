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
import { describeError, useControlResource } from "../../hooks/useControlResource";
import {
  SIMULATE_REASON_TEXT,
  buildEdits,
  describeOperation,
  duplicateCells,
  isEditableTable,
  loadEditorState,
  pendingCellSet,
  previewRefusal,
  saveEditorState,
  simulateReason,
  specFileName,
} from "../../lib/romEditor";
import {
  ROM_UPLOAD_LIMIT,
  describeEditorDisagreement,
  entryTitle,
  refusalText,
} from "../../lib/romLibrary";
import { SIM_BLOCK_TEXT, resultFileName, simFailure } from "../../lib/romSim";
import { buttonClass, cn } from "../../styles";
import type { StatusResponse } from "../../types";
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
import type { RomLibraryEntry } from "../../types/romLibrary";
import { downloadText } from "../../util/browser";
import { BasePicker, SimulateChip } from "./romEditor/BasePicker";
import { CellGrid } from "./romEditor/CellGrid";
import { OpenRom } from "./romEditor/OpenRom";
import { OperationPanel } from "./romEditor/OperationPanel";
import { PendingEdits } from "./romEditor/PendingEdits";
import { PreviewResult } from "./romEditor/PreviewResult";
import { SimFailureAlert, SimForm, SimRunning } from "./romEditor/SimForm";
import { SimResults } from "./romEditor/SimResults";
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

/** Engine Tuning ▸ ROM Editor (W09.3): pick a stored image, edit the fuel
 *  chain in Map 1, preview the result, download the edit spec. The edited
 *  image never leaves the server: it is not stored, not downloadable, and not
 *  flashable. A flash is the Flash page's explicit act. */
export function RomEditorPage({ token }: { token: string; status: StatusResponse | null }) {
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
  return (
    <section className="space-y-4">
      <Panel
        title="ROM Editor"
        count={library.data ? `${entries.length} ${entries.length === 1 ? "image" : "images"}` : ""}
      >
        <div className="space-y-4">
          <p className="text-sm text-fg-dim">
            Edit the fuel chain (Fuel Targets, Fuel Compensation and K, Map 1) of a ROM and see the
            tables after the edits. The edited image is never stored or offered: you get an edit
            spec, a reviewable file that reproduces the edit from its base. Nothing here writes the
            ECU.
          </p>
          {!token && <p role="status">Enter the control token to use the ROM Editor.</p>}
          {library.error && <Notice notice={{ kind: "error", text: library.error }} />}
          <OpenRom
            label={nothingLoaded ? "Open ROM…" : "Open another ROM…"}
            primary={nothingLoaded && !loading}
            disabled={!enabled || !!busy}
            dragging={dragging}
            onDraggingChange={setDragging}
            onFile={(file) => void openFile(file)}
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
          {recent.length > 0 && (
            <div className="space-y-2">
              <h3 className="text-sm font-semibold">Recent ROMs</h3>
              <BasePicker
                entries={recent}
                selectedSha={selectedSha}
                onChoose={(sha) => {
                  setChosen(sha);
                  writeShaToUrl(sha);
                }}
              />
            </div>
          )}
        </div>
      </Panel>
      {selected && <Workspace key={selected.sha256} token={token} entry={selected} />}
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

type SimState =
  | { kind: "idle" }
  | { kind: "running"; startedAt: number }
  | { kind: "ok"; result: RomSimResult; run: SimRunContext }
  | { kind: "failed"; failure: SimFailure };

function Workspace({ token, entry }: { token: string; entry: RomLibraryEntry }) {
  const sha = entry.sha256;
  const tables = useControlResource(
    useCallback((signal: AbortSignal) => getRomTables(token, sha, signal), [token, sha]),
    { enabled: Boolean(token), intervalMs: null },
  );
  const editable = (tables.data?.tables ?? []).filter(isEditableTable);
  const [tableId, setTableId] = useState<string | null>(null);
  const table = editable.find((item) => item.id === tableId) ?? editable[0] ?? null;

  const [selection, setSelection] = useState<CellSelection | null>(null);
  const [saved] = useState(() => loadEditorState(sha));
  const [pending, setPending] = useState<PendingEdit[]>(saved.pending);
  const [label, setLabel] = useState(saved.label);
  const [opRefusal, setOpRefusal] = useState<{ reason: string; message: string } | null>(null);
  const [preview, setPreview] = useState<PreviewState>({ kind: "idle" });
  const request = useRef(0);
  const counter = useRef(0);
  const [sim, setSim] = useState<SimState>({ kind: "idle" });
  const simRun = useRef(0);
  const simAbort = useRef<AbortController | null>(null);
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
    setSim({ kind: "running", startedAt });
    try {
      const result = await simulateRomEdits(token, sha, simRequest, controller.signal);
      if (simRun.current !== mine) return;
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
          elapsedS: Math.round((Date.now() - startedAt) / 1000),
        },
      });
    } catch (err) {
      if (simRun.current !== mine) return;
      setSim({ kind: "failed", failure: simFailure(err) });
    }
  }

  if (tables.loading || tables.stale) return <p className="text-fg-dim">Reading tables…</p>;
  if (tables.error) return <Notice notice={{ kind: "error", text: tables.error }} />;
  if (!table) {
    return (
      <Panel title="Edit">
        <p className="text-fg-dim">
          {tables.data?.reason ?? "This image has no editable fuel table in Map 1."}
        </p>
      </Panel>
    );
  }

  const baseTables = tables.data?.tables ?? [];
  const decodeLine = [
    table.dtype,
    table.decoded ? (table.decode?.formula ?? null) : "raw: decode unconfirmed",
    table.confidence,
  ]
    .filter(Boolean)
    .join(" · ");
  const rowAxis = table.axes.rows;
  const colAxis = table.axes.cols;
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

  return (
    <div className="space-y-4">
      <Panel title="Edit">
        <div className="space-y-4">
          <div role="group" aria-label="Table" className="flex flex-wrap gap-2">
            {editable.map((item) => (
              <button
                key={item.id}
                type="button"
                className={cn(buttonClass, item.id === table.id && "border-accent-2")}
                aria-pressed={item.id === table.id}
                onClick={() => {
                  setTableId(item.id ?? null);
                  setSelection(null);
                  setOpRefusal(null);
                }}
              >
                {item.label ?? item.name}
              </button>
            ))}
          </div>
          <p className="font-num text-xs text-fg-faint">{decodeLine}</p>
          <p className="text-xs text-fg-dim">
            {rowAxis
              ? `Rows: ${rowAxis.name}${rowAxis.unit ? ` (${rowAxis.unit})` : ""}`
              : "Rows: index"}
            {" · "}
            {colAxis
              ? `Columns: ${colAxis.name}${colAxis.unit ? ` (${colAxis.unit})` : ""}`
              : "Columns: index"}
            {" · "}Cells: {table.decoded ? (table.unit ?? "value") : "raw"}
          </p>
          {table.editor_disagreement && (
            <p role="note" aria-label={`${table.name} editor labels`} className="text-sm text-warn">
              {describeEditorDisagreement(table.editor_disagreement)}
            </p>
          )}
          <p className="text-xs text-fg-faint">
            Click a cell, shift-click or drag for a range; arrows move, shift+arrows extend. Dashed
            cells are in a pending edit.
          </p>
          <CellGrid
            key={`grid-${table.id}`}
            table={table}
            label={`${table.label ?? table.name} cells`}
            selection={selection}
            onSelect={setSelection}
            pendingCells={pendingCellSet(pending, table.id ?? "")}
          />
          <OperationPanel
            key={`operation-${table.id}`}
            table={table}
            selection={selection}
            refusal={opRefusal}
            onApply={apply}
          />
        </div>
      </Panel>

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

      <section aria-label="Simulate" data-slot="simulate" className="space-y-4">
        <Panel title="Simulate">
          <div className="space-y-4">
            <SimForm
              tables={baseTables}
              label={label}
              edits={edits}
              blocked={simBlocked}
              startedAt={sim.kind === "running" ? sim.startedAt : null}
              onRun={(simRequest) => void runSimulation(simRequest)}
            />
            {sim.kind === "running" && <SimRunning startedAt={sim.startedAt} />}
            {sim.kind === "failed" && <SimFailureAlert failure={sim.failure} />}
          </div>
        </Panel>
        {sim.kind === "ok" && (
          <Panel title="Simulation result">
            <SimResults
              result={sim.result}
              run={sim.run}
              stale={JSON.stringify(sim.run.request.edits) !== JSON.stringify(edits)}
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

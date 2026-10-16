import { useCallback, useEffect, useRef, useState } from "react";

import { getRomLibrary, getRomTables, previewRomEdits } from "../../api/workbench";
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
import { describeEditorDisagreement } from "../../lib/romLibrary";
import { buttonClass, cn } from "../../styles";
import type { StatusResponse } from "../../types";
import type {
  CellSelection,
  PendingEdit,
  RomEditOperation,
  RomEditPreview,
  RomEditRefusal,
} from "../../types/romEditor";
import type { RomLibraryEntry } from "../../types/romLibrary";
import { downloadText } from "../../util/browser";
import { BasePicker } from "./romEditor/BasePicker";
import { CellGrid } from "./romEditor/CellGrid";
import { OperationPanel } from "./romEditor/OperationPanel";
import { PendingEdits } from "./romEditor/PendingEdits";
import { PreviewResult } from "./romEditor/PreviewResult";

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
  const entries = library.data?.entries ?? [];
  const [chosen, setChosen] = useState<string | null>(shaFromUrl);
  const selected = entries.find((entry) => entry.sha256 === chosen) ?? entries[0] ?? null;
  const selectedSha = selected?.sha256 ?? null;
  useEffect(() => {
    if (selectedSha) writeShaToUrl(selectedSha);
  }, [selectedSha]);

  const empty = enabled && !library.loading && !library.error && entries.length === 0;
  return (
    <section className="space-y-4">
      <Panel
        title="ROM Editor"
        count={library.data ? `${entries.length} ${entries.length === 1 ? "image" : "images"}` : ""}
      >
        <div className="space-y-4">
          <p className="text-sm text-fg-dim">
            Edit the fuel chain (Fuel Targets, Fuel Compensation and K, Map 1) of a stored image and
            see the tables after the edits. The edited image is never stored or offered: you get an
            edit spec, a reviewable file that reproduces the edit from its base. Nothing here writes
            the ECU.
          </p>
          {!token && <p role="status">Enter the control token to use the ROM Editor.</p>}
          {library.error && <Notice notice={{ kind: "error", text: library.error }} />}
          {library.loading && enabled && <p className="text-fg-dim">Reading the library…</p>}
          {empty && (
            <p role="status" className="rounded-lg border border-border p-4">
              The library is empty. Upload a plain image or an .OsirisROM on the ROM Library page
              first.
            </p>
          )}
          {entries.length > 0 && (
            <BasePicker
              entries={entries}
              selectedSha={selectedSha}
              onChoose={(sha) => {
                setChosen(sha);
                writeShaToUrl(sha);
              }}
            />
          )}
        </div>
      </Panel>
      {selected && <Workspace key={selected.sha256} token={token} entry={selected} />}
    </section>
  );
}

type PreviewState =
  | { kind: "idle" }
  | { kind: "busy" }
  | { kind: "ok"; data: RomEditPreview }
  | { kind: "refused"; refusal: RomEditRefusal }
  | { kind: "error"; text: string };

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
  const simBlocked =
    preview.kind === "ok" ? preview.data.sim_base.reason : simulateReason(entry.revision_match);

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

      {/* W09.4: the Simulate form (SimForm) and results (SimResults) go in this
          section. W09.3 builds only the slot and the reason it is disabled. */}
      <section aria-label="Simulate" data-slot="simulate" className="space-y-2">
        {simBlocked && (
          <p role="note" className="text-sm text-warn">
            Simulate is unavailable: {simBlocked}. {SIMULATE_REASON_TEXT[simBlocked] ?? ""}
          </p>
        )}
      </section>
    </div>
  );
}

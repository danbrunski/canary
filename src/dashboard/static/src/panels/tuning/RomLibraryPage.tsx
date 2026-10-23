import { useCallback, useRef, useState, type DragEvent, type ReactNode } from "react";
import { TableScroll } from "../../components/TableScroll";

import {
  deleteRomImage,
  downloadRomImage,
  getRomDescription,
  getRomLibrary,
  renameRomImage,
  uploadRomImage,
} from "../../api/workbench";
import { Notice } from "../../components/Notice";
import { Panel } from "../../components/Panel";
import { useAsyncAction } from "../../hooks/useAsyncAction";
import { useControlResource } from "../../hooks/useControlResource";
import {
  SOURCE_TEXT,
  absoluteRegions,
  downloadName,
  entrySources,
  entryTitle,
  formatBytes,
  hexAddress,
  refusalText,
  romFileProblem,
} from "../../lib/romLibrary";
import {
  buttonClass,
  cn,
  dangerButtonClass,
  definitionListClass,
  definitionRowClass,
  definitionTermClass,
  definitionValueClass,
  inputClass,
} from "../../styles";
import type { StatusResponse } from "../../types";
import type { RomLibraryEntry } from "../../types/romLibrary";
import { downloadBlob } from "../../util/browser";
import { EcuBackupCard } from "./EcuBackupCard";
import { RomBlockGrid, RomRegionTable } from "./RomMemoryMap";
import { RomCompare } from "./RomCompare";
import { RomTablesView } from "./RomTablesView";
import {
  ChecksumChip,
  ExtentChip,
  RevisionChip,
  UnverifiedChip,
  checksumWords,
} from "./romLibraryParts";

/** Engine Tuning ▸ ROM Library (W08.6, W08.8): stored ROM images, the memory
 *  map of the selected one, its tables, compare, and the ECU backup.
 *  Read-only throughout; it stands alone and assumes no other page ran. */
export function RomLibraryPage({
  token,
  status,
}: {
  token: string;
  status: StatusResponse | null;
}) {
  const enabled = Boolean(token);
  const library = useControlResource(
    useCallback((signal: AbortSignal) => getRomLibrary(token, signal), [token]),
    { enabled, intervalMs: null },
  );
  const entries = library.data?.entries ?? [];
  const [chosen, setChosen] = useState<string | null>(null);
  const selected = entries.find((entry) => entry.sha256 === chosen) ?? entries[0] ?? null;
  const { notice, setNotice, busy, run } = useAsyncAction();
  const [renaming, setRenaming] = useState<{ sha: string; value: string } | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const backup = useRef<HTMLDivElement>(null);
  const refresh = library.refresh;

  function upload(file: File | undefined) {
    if (!file) return;
    const problem = romFileProblem(file);
    if (problem) {
      setNotice({ kind: "error", text: problem });
      return;
    }
    return run(
      `Uploading ${file.name}`,
      async () => {
        try {
          const entry = await uploadRomImage(token, file);
          setChosen(entry.sha256);
          const what = `calibration ${entry.calibration_id ?? "not readable"}, sha256 ${entry.sha256.slice(0, 12)}…`;
          setNotice({
            kind: "ok",
            text:
              entry.created === false
                ? `${file.name} is already in the library (${what}); its source was added.`
                : `Stored ${file.name}: ${what}.`,
          });
        } catch (err) {
          setNotice({ kind: "error", text: `${file.name}: ${refusalText(err)}` });
        }
      },
      { okText: null, then: refresh },
    );
  }

  function drop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    if (!enabled || busy) return;
    void upload(event.dataTransfer.files[0]);
  }

  function saveLabel(sha: string, value: string) {
    return run(
      "Renaming",
      async () => {
        await renameRomImage(token, sha, value.trim() || null);
        setRenaming(null);
      },
      { okText: null, then: refresh },
    );
  }

  function remove(entry: RomLibraryEntry) {
    const title = entryTitle(entry);
    return run(
      `Deleting ${title}`,
      async () => {
        await deleteRomImage(token, entry.sha256);
        setDeleting(null);
        if (chosen === entry.sha256) setChosen(null);
      },
      { okText: `Deleted ${title}.`, then: refresh },
    );
  }

  function download(entry: RomLibraryEntry) {
    return run(
      `Downloading ${entryTitle(entry)}`,
      async () => downloadBlob(await downloadRomImage(token, entry.sha256), downloadName(entry)),
      { okText: null },
    );
  }

  function goToBackup() {
    const element = backup.current;
    element?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    element?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
  }

  const empty = enabled && !library.loading && !library.error && entries.length === 0;
  return (
    <section className="space-y-4">
      <Panel
        title="ROM library"
        count={
          library.data
            ? `${entries.length} ${entries.length === 1 ? "image" : "images"} · read-only`
            : "Read-only"
        }
      >
        <div className="space-y-4">
          <p className="text-sm text-fg-dim">
            ROM images stored for this vehicle, by the sha256 of the plain image. An ECU read stores
            the plain image directly, and an .OsirisROM is decoded to the same bytes. A format the
            vehicle&apos;s map definition recognises but cannot decode, such as UpRev&apos;s
            encrypted backup .bin, is refused by name. Nothing here writes the ECU.
          </p>
          {!token && <p role="status">Enter the control token to use the ROM library.</p>}
          {library.error && <Notice notice={{ kind: "error", text: library.error }} />}
          <div
            role="group"
            aria-label="Upload a ROM image"
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={drop}
            className={cn(
              "rounded-lg border border-dashed p-4",
              dragging ? "border-accent-2 bg-accent-2/5" : "border-border",
            )}
          >
            <label className="flex flex-col gap-2 text-sm">
              Drop a plain image (.bin) or an .OsirisROM here, or choose one:
              <input
                ref={fileInput}
                aria-label="ROM image file"
                className={inputClass}
                type="file"
                accept=".bin,.OsirisROM,.osirisrom"
                disabled={!enabled || !!busy}
                onChange={(event) => {
                  void upload(event.target.files?.[0]);
                  event.target.value = "";
                }}
              />
            </label>
          </div>
          {busy ? (
            <Notice notice={{ kind: "busy", text: `${busy}…` }} />
          ) : (
            <Notice notice={notice} />
          )}
          {library.loading && enabled && <p className="text-fg-dim">Reading the library…</p>}
          {empty && (
            <div role="status" className="space-y-2 rounded-lg border border-border p-4">
              <p>
                The library is empty: no ROM image has been uploaded or read from this
                vehicle&apos;s ECU yet.
              </p>
              <p className="text-sm text-fg-dim">
                Upload a plain image (.bin) or an .OsirisROM, or back up the ECU (key on, engine
                off, about 10 minutes).
              </p>
              <div className="flex flex-wrap gap-2">
                <button className={buttonClass} onClick={() => fileInput.current?.click()}>
                  Upload an image
                </button>
                <button className={buttonClass} onClick={goToBackup}>
                  Back up the ECU
                </button>
              </div>
            </div>
          )}
          {entries.length > 0 && (
            <TableScroll>
              <table className="w-full min-w-[48rem] text-left text-sm" aria-label="ROM images">
                <thead className="text-[0.7rem] uppercase tracking-[0.08em] text-fg-faint">
                  <tr>
                    <th className="py-2 pr-3">Label</th>
                    <th className="py-2 pr-3">Source</th>
                    <th className="py-2 pr-3">Calibration</th>
                    <th className="py-2 pr-3">Revision</th>
                    <th className="py-2 pr-3">Checksum</th>
                    <th className="py-2 pr-3">Added</th>
                    <th className="py-2">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {entries.map((entry) => (
                    <EntryRow
                      key={entry.sha256}
                      entry={entry}
                      selected={selected?.sha256 === entry.sha256}
                      busy={!!busy}
                      renaming={renaming?.sha === entry.sha256 ? renaming.value : null}
                      deleting={deleting === entry.sha256}
                      onSelect={() => setChosen(entry.sha256)}
                      onRenameStart={() =>
                        setRenaming({ sha: entry.sha256, value: entry.label ?? "" })
                      }
                      onRenameChange={(value) => setRenaming({ sha: entry.sha256, value })}
                      onRenameSave={() => void saveLabel(entry.sha256, renaming?.value ?? "")}
                      onRenameCancel={() => setRenaming(null)}
                      onDeleteAsk={() => setDeleting(entry.sha256)}
                      onDeleteConfirm={() => void remove(entry)}
                      onDeleteCancel={() => setDeleting(null)}
                      onDownload={() => void download(entry)}
                    />
                  ))}
                </tbody>
              </table>
            </TableScroll>
          )}
        </div>
      </Panel>

      <div ref={backup}>
        <EcuBackupCard
          token={token}
          status={status}
          entries={entries}
          onOpenEntry={setChosen}
          onLibraryChanged={refresh}
        />
      </div>

      {selected && <RomImageView key={selected.sha256} token={token} entry={selected} />}

      <RomCompare token={token} entries={entries} />
    </section>
  );
}

function EntryRow({
  entry,
  selected,
  busy,
  renaming,
  deleting,
  onSelect,
  onRenameStart,
  onRenameChange,
  onRenameSave,
  onRenameCancel,
  onDeleteAsk,
  onDeleteConfirm,
  onDeleteCancel,
  onDownload,
}: {
  entry: RomLibraryEntry;
  selected: boolean;
  busy: boolean;
  renaming: string | null;
  deleting: boolean;
  onSelect: () => void;
  onRenameStart: () => void;
  onRenameChange: (value: string) => void;
  onRenameSave: () => void;
  onRenameCancel: () => void;
  onDeleteAsk: () => void;
  onDeleteConfirm: () => void;
  onDeleteCancel: () => void;
  onDownload: () => void;
}) {
  const title = entryTitle(entry);
  return (
    <>
      <tr className={cn("border-t border-border align-top", selected && "bg-elev-2/60")}>
        <td className="py-2 pr-3">
          {renaming !== null ? (
            <form
              className="flex flex-wrap gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                onRenameSave();
              }}
            >
              <input
                aria-label={`Label for ${title}`}
                className={inputClass}
                value={renaming}
                onChange={(event) => onRenameChange(event.target.value)}
              />
              <button type="submit" className={buttonClass} disabled={busy}>
                Save
              </button>
              <button type="button" className={buttonClass} onClick={onRenameCancel}>
                Cancel
              </button>
            </form>
          ) : (
            <button
              type="button"
              className="text-left font-semibold hover:text-accent-2"
              aria-current={selected ? "true" : undefined}
              onClick={onSelect}
            >
              {title}
            </button>
          )}
        </td>
        <td className="py-2 pr-3 text-fg-dim">
          {entrySources(entry)
            .map((source) => SOURCE_TEXT[source] ?? source)
            .join(", ")}
        </td>
        <td className="py-2 pr-3 font-num">{entry.calibration_id ?? "not readable"}</td>
        <td className="py-2 pr-3">
          <span className="flex flex-wrap gap-1">
            <RevisionChip match={entry.revision_match} />
            <UnverifiedChip entry={entry} />
            {entry.extent?.verdict === "larger" && <ExtentChip extent={entry.extent} />}
          </span>
        </td>
        <td className="py-2 pr-3">
          <ChecksumChip checksum={entry.checksum} />
        </td>
        <td className="whitespace-nowrap py-2 pr-3 text-fg-dim">
          {new Date(entry.added_at).toLocaleString()}
        </td>
        <td className="py-2">
          <span className="flex flex-wrap gap-1">
            <button
              type="button"
              className={buttonClass}
              disabled={busy}
              aria-label={`Rename ${title}`}
              onClick={onRenameStart}
            >
              Rename
            </button>
            <button
              type="button"
              className={buttonClass}
              disabled={busy}
              aria-label={`Download ${title}`}
              onClick={onDownload}
            >
              Download
            </button>
            <button
              type="button"
              className={dangerButtonClass}
              disabled={busy}
              aria-label={`Delete ${title}`}
              onClick={onDeleteAsk}
            >
              Delete
            </button>
          </span>
        </td>
      </tr>
      {deleting && (
        <tr>
          <td colSpan={7} className="pb-3">
            <div
              role="group"
              aria-label={`Confirm delete ${title}`}
              className="flex flex-wrap items-center gap-2 rounded-lg border border-bad/55 p-3"
            >
              <span>
                Delete {title} ({entry.sha256.slice(0, 12)}…) from the library? The stored image is
                removed; download it first to keep a copy.
              </span>
              <button
                type="button"
                className={dangerButtonClass}
                disabled={busy}
                onClick={onDeleteConfirm}
              >
                Delete image
              </button>
              <button type="button" className={buttonClass} onClick={onDeleteCancel}>
                Keep
              </button>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

/** The selected image: identity, memory map, regions and tables. */
function RomImageView({ token, entry }: { token: string; entry: RomLibraryEntry }) {
  const described = useControlResource(
    useCallback(
      (signal: AbortSignal) => getRomDescription(token, entry.sha256, signal),
      [token, entry.sha256],
    ),
    { enabled: Boolean(token), intervalMs: null },
  );
  const description = described.stale ? null : described.data;
  const reading = described.loading || described.stale;
  const noMap =
    description && !description.map
      ? (description.map_reason ??
        `No map definition for calibration ${description.calibration_id ?? "(unreadable)"}`)
      : null;
  const meta = entry.read_meta;
  const title = entryTitle(entry);
  const pending = reading ? (
    <p className="text-fg-dim">Reading {title}…</p>
  ) : described.error ? (
    <p className="text-bad">{described.error}</p>
  ) : null;
  return (
    <>
      <Panel title="Image" count={title}>
        <dl className={definitionListClass}>
          <Fact term="Calibration">
            {entry.calibration_id ?? "not readable at the declared offset"}
          </Fact>
          <Fact term="Revision">
            <RevisionChip match={entry.revision_match} />
          </Fact>
          <Fact term="Checksum">
            <span className="inline-flex flex-wrap items-center justify-end gap-2">
              <ChecksumChip checksum={entry.checksum} />
              {entry.checksum && (
                <span className="text-xs text-fg-dim">{checksumWords(entry.checksum)}</span>
              )}
            </span>
          </Fact>
          <Fact term="Size">
            {entry.size.toLocaleString()} bytes ({hexAddress(0)}–
            {hexAddress(Math.max(0, entry.size - 1))})
          </Fact>
          {entry.extent && (
            <Fact term="Whole flash?">
              <span className="inline-flex flex-wrap items-center justify-end gap-2">
                <ExtentChip extent={entry.extent} />
                <span className="text-xs text-fg-dim">{entry.extent.summary}</span>
              </span>
            </Fact>
          )}
          <Fact term="Image sha256">
            <span className="break-all text-xs">{entry.sha256}</span>
          </Fact>
          <Fact term="CRC32">{entry.crc32}</Fact>
          {entry.container_sha256 && (
            <Fact term=".OsirisROM sha256">
              <span className="break-all text-xs">{entry.container_sha256}</span>
            </Fact>
          )}
          <Fact term="Source">
            {entrySources(entry)
              .map((source) => SOURCE_TEXT[source] ?? source)
              .join(", ")}
            {entry.filename ? ` · ${entry.filename}` : ""}
          </Fact>
          <Fact term="Added">{new Date(entry.added_at).toLocaleString()}</Fact>
          {meta && (
            <Fact term="ECU read">
              {[
                meta.duration_s != null
                  ? `${formatBytes(entry.size)} in ${Math.round(meta.duration_s)} s`
                  : null,
                meta.requests != null ? `${meta.requests.toLocaleString()} requests` : null,
                meta.retries != null ? `${meta.retries} retries` : null,
                meta.verified === false || meta.unverified_reason
                  ? `unverified: ${meta.unverified_reason ?? "no reason recorded"}`
                  : meta.verified
                    ? "verified"
                    : null,
              ]
                .filter(Boolean)
                .join(" · ")}
            </Fact>
          )}
        </dl>
        {entry.checksum && !entry.checksum.ok && (
          <p className="mt-3 text-sm text-bad">
            The checksum fails{entry.checksum.reason ? `: ${entry.checksum.reason}` : ""}. The image
            is shown, not refused.
          </p>
        )}
      </Panel>

      <Panel
        title="Memory map"
        count={description ? `${description.blocks.length} blocks` : undefined}
      >
        {pending ??
          (description && (
            <div className="space-y-2">
              <p className="text-sm text-fg-dim">
                Each square is 4 KiB and each row 64 KiB. Colour shows what the bytes look like
                statistically, not what they are. Hover over, tap or focus a square for its address
                and numbers.
              </p>
              <RomBlockGrid blocks={description.blocks} regions={description.regions} />
            </div>
          ))}
      </Panel>

      <Panel
        title="Regions"
        count={description?.map ? absoluteRegions(description.regions).length : undefined}
      >
        {pending ??
          (description &&
            (noMap ? (
              <p className="text-fg-dim">
                {noMap}: the block statistics above are all this image has; no regions are mapped.
              </p>
            ) : (
              <RomRegionTable
                regions={absoluteRegions(description.regions)}
                eraseSectors={description.map?.erase_sectors}
              />
            )))}
      </Panel>

      <Panel title="Tables">
        {pending ??
          (description && (
            <RomTablesView
              token={token}
              sha={entry.sha256}
              mapSlots={description.map_slots}
              regions={description.regions}
              noMapReason={noMap}
            />
          ))}
      </Panel>
    </>
  );
}

function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className={definitionRowClass}>
      <dt className={definitionTermClass}>{term}</dt>
      <dd className={cn(definitionValueClass, "text-right")}>{children}</dd>
    </div>
  );
}

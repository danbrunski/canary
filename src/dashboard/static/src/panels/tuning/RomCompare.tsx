import { useEffect, useRef, useState } from "react";

import { compareRomImages } from "../../api/workbench";
import { Notice } from "../../components/Notice";
import { Panel } from "../../components/Panel";
import { entryTitle, formatCell, hexSpan, refusalText } from "../../lib/romLibrary";
import { buttonClass, inputClass } from "../../styles";
import type { RomCompareCell, RomCompareResponse, RomLibraryEntry } from "../../types/romLibrary";

function cellValue(raw: number, decoded: number | null | undefined, unit?: string | null): string {
  return decoded == null
    ? formatCell(raw)
    : `${formatCell(decoded)}${unit ? ` ${unit}` : ""} (raw ${formatCell(raw)})`;
}

/** Pick two library images and list what changed from the first to the
 *  second: byte ranges, the regions and tables each overlaps, and per-cell
 *  old → new values inside tables (W08.6). */
export function RomCompare({ token, entries }: { token: string; entries: RomLibraryEntry[] }) {
  const [from, setFrom] = useState<string>("");
  const [to, setTo] = useState<string>("");
  const [result, setResult] = useState<RomCompareResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);

  const known = new Set(entries.map((entry) => entry.sha256));
  // Older → newer by default: entries arrive newest first.
  const a = known.has(from) ? from : (entries[1]?.sha256 ?? "");
  const b = known.has(to) ? to : (entries[0]?.sha256 ?? "");
  const title = (sha: string) => {
    const entry = entries.find((item) => item.sha256 === sha);
    return entry ? entryTitle(entry) : sha.slice(0, 12);
  };

  async function compare() {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const data = await compareRomImages(token, a, b, controller.signal);
      if (!controller.signal.aborted) setResult(data);
    } catch (err) {
      if (!controller.signal.aborted) setError(refusalText(err));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  const blocker = !token
    ? "Enter the control token to compare images."
    : entries.length < 2
      ? "Compare needs two images in the library."
      : !a || !b || a === b
        ? "Pick two different images."
        : null;
  const ranges = result?.ranges ?? [];
  const spanned = ranges.reduce((sum, range) => sum + range.length, 0);
  return (
    <Panel title="Compare" count={result ? `${ranges.length} ranges` : undefined}>
      <div className="space-y-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            From
            <select
              aria-label="Compare from"
              className={inputClass}
              value={a}
              disabled={entries.length < 2}
              onChange={(event) => {
                setFrom(event.target.value);
                setResult(null);
              }}
            >
              {entries.map((entry) => (
                <option key={entry.sha256} value={entry.sha256}>
                  {entryTitle(entry)}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            To
            <select
              aria-label="Compare to"
              className={inputClass}
              value={b}
              disabled={entries.length < 2}
              onChange={(event) => {
                setTo(event.target.value);
                setResult(null);
              }}
            >
              {entries.map((entry) => (
                <option key={entry.sha256} value={entry.sha256}>
                  {entryTitle(entry)}
                </option>
              ))}
            </select>
          </label>
          <button
            className={buttonClass}
            disabled={!!blocker || busy}
            onClick={() => void compare()}
          >
            Compare
          </button>
        </div>
        {blocker && <p className="text-sm text-fg-dim">{blocker}</p>}
        {busy && <Notice notice={{ kind: "busy", text: "Comparing…" }} />}
        {error && <Notice notice={{ kind: "error", text: error }} />}
        {result && (
          <div className="space-y-3" aria-label="Compare result">
            <p className="text-sm">
              {title(result.a)} → {title(result.b)}:{" "}
              {ranges.length === 0
                ? "the images are identical."
                : result.changed_bytes != null
                  ? `${result.changed_bytes} bytes changed in ${ranges.length} ranges.`
                  : `${ranges.length} ranges spanning ${spanned} bytes.`}
            </p>
            {result.map_applied === false && (
              <p className="text-sm text-fg-dim">
                Ranges are unlabelled:{" "}
                {result.map_reason ?? "the map does not describe both images"}.
              </p>
            )}
            <ol className="space-y-3">
              {ranges.map((range) => (
                <li key={range.start} className="rounded-lg border border-border p-3 text-sm">
                  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                    <span className="font-num text-accent-2">
                      {hexSpan(range.start, range.end)}
                    </span>
                    <span className="font-num text-xs text-fg-faint">
                      {range.changed != null && range.changed !== range.length
                        ? `${range.changed} of ${range.length} B changed`
                        : `${range.length} B`}
                    </span>
                    <span>{range.labels.length ? range.labels.join(" · ") : "unmapped bytes"}</span>
                  </div>
                  {range.cells && range.cells.length > 0 && <CellChanges cells={range.cells} />}
                </li>
              ))}
            </ol>
          </div>
        )}
      </div>
    </Panel>
  );
}

function CellChanges({ cells }: { cells: RomCompareCell[] }) {
  const tableOf = (cell: RomCompareCell) => cell.table_label ?? cell.table ?? "";
  const withTable = cells.some((cell) => tableOf(cell));
  return (
    <div className="mt-2 overflow-x-auto">
      <table className="text-left font-num text-xs" aria-label="Changed cells">
        <thead className="text-fg-faint">
          <tr>
            {withTable && <th className="pr-4">Table</th>}
            <th className="pr-4">Row</th>
            <th className="pr-4">Col</th>
            <th className="pr-4">Old</th>
            <th className="pr-4">New</th>
          </tr>
        </thead>
        <tbody>
          {cells.map((cell, index) => (
            <tr key={`${cell.table ?? ""}-${cell.row}-${cell.col}-${index}`}>
              {withTable && <td className="pr-4">{tableOf(cell)}</td>}
              <td className="pr-4">{cell.row}</td>
              <td className="pr-4">{cell.col}</td>
              <td className="pr-4">{cellValue(cell.old, cell.old_decoded, cell.unit)}</td>
              <td className="pr-4">{cellValue(cell.new, cell.new_decoded, cell.unit)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

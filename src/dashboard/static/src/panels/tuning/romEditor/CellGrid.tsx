import { useEffect, useRef } from "react";

import { TableScroll } from "../../../components/TableScroll";
import {
  formatDecoded,
  isArrowKey,
  moveSelection,
  selectionContains,
} from "../../../lib/romEditor";
import { formatCell } from "../../../lib/romLibrary";
import { cn } from "../../../styles";
import type { CellSelection } from "../../../types/romEditor";
import type { RomTableValues } from "../../../types/romLibrary";

/** The value a cell shows: decoded where the table has a decode, else raw. */
function shown(table: RomTableValues, row: number, col: number): number | null {
  return table.decoded ? (table.decoded[row]?.[col] ?? null) : (table.raw[row]?.[col] ?? null);
}

/** Change from the base table's cell to this one, in percent of the base
 *  value (decoded when both are decoded, else raw). Null when it cannot be
 *  stated: a base of zero or no value. */
export function deltaPercent(
  table: RomTableValues,
  base: RomTableValues,
  row: number,
  col: number,
): number | null {
  const before = shown(base, row, col);
  const after = shown(table, row, col);
  if (before === null || after === null || before === 0) return null;
  return ((after - before) / Math.abs(before)) * 100;
}

function axisLabels(table: RomTableValues, axis: "rows" | "cols"): string[] | null {
  const found = table.axes[axis];
  if (!found) return null;
  return (found.decoded ?? found.raw).map((value) => formatCell(value ?? null));
}

/** One table as a grid of cells, decoded with the raw count on hover.
 *
 *  Two uses. Interactive (`onSelect` given): click selects a cell,
 *  shift-click or drag a rectangle, arrows move and shift+arrows extend,
 *  and cells a pending edit already selects are dashed. Read-only after
 *  view (`base` given): cells that differ from `base` are outlined and carry
 *  their change in percent. */
export function CellGrid({
  table,
  label,
  selection = null,
  onSelect,
  pendingCells,
  base,
}: {
  table: RomTableValues;
  label: string;
  selection?: CellSelection | null;
  onSelect?: (selection: CellSelection | null) => void;
  pendingCells?: Set<string>;
  base?: RomTableValues;
}) {
  const dragging = useRef(false);
  const element = useRef<HTMLTableElement>(null);
  useEffect(() => {
    const stop = () => {
      dragging.current = false;
    };
    window.addEventListener("mouseup", stop);
    return () => window.removeEventListener("mouseup", stop);
  }, []);

  const rows = table.raw.length;
  const cols = table.raw[0]?.length ?? 0;
  const rowLabels = axisLabels(table, "rows");
  const colLabels = axisLabels(table, "cols");
  const interactive = onSelect !== undefined;
  const unit = table.decoded ? (table.unit ?? "") : "raw";

  return (
    <TableScroll className="rounded-lg border border-border">
      <table
        ref={element}
        role={interactive ? "grid" : "table"}
        aria-label={label}
        aria-multiselectable={interactive ? true : undefined}
        tabIndex={interactive ? 0 : undefined}
        className="w-full select-none border-separate border-spacing-1 text-center font-num text-xs outline-none focus-visible:ring-1 focus-visible:ring-accent-2"
        onKeyDown={
          interactive
            ? (event) => {
                if (event.key === "Escape") {
                  onSelect(null);
                } else if (isArrowKey(event.key)) {
                  event.preventDefault();
                  onSelect(moveSelection(selection, event.key, event.shiftKey, rows, cols));
                }
              }
            : undefined
        }
      >
        <caption className="sr-only">
          Cells in {unit || "value"}; the raw count is on hover.
        </caption>
        {colLabels && (
          <thead>
            <tr>
              <th scope="col" className="p-1" />
              {colLabels.map((name, c) => (
                <th key={c} scope="col" className="p-1 text-fg-dim">
                  {name}
                </th>
              ))}
            </tr>
          </thead>
        )}
        <tbody>
          {table.raw.map((rawRow, r) => (
            <tr key={r}>
              <th scope="row" className="p-1 text-fg-dim">
                {rowLabels ? (rowLabels[r] ?? r + 1) : r + 1}
              </th>
              {rawRow.map((raw, c) => {
                const value = shown(table, r, c);
                const changed = base ? raw !== base.raw[r]?.[c] : false;
                const delta = base && changed ? deltaPercent(table, base, r, c) : null;
                const selected = selectionContains(selection, r, c);
                const pending = pendingCells?.has(`${r}:${c}`) ?? false;
                const text = table.decoded ? formatDecoded(value) : formatCell(value);
                return (
                  <td
                    key={c}
                    role={interactive ? "gridcell" : undefined}
                    aria-selected={interactive ? selected : undefined}
                    aria-label={`Row ${r + 1}, column ${c + 1}: ${text}${
                      changed ? " (changed)" : ""
                    }`}
                    data-row={r}
                    data-col={c}
                    data-changed={changed || undefined}
                    data-pending={pending || undefined}
                    title={`raw ${raw}${base && changed ? ` (was ${base.raw[r]?.[c]})` : ""}`}
                    className={cn(
                      "min-w-12 rounded p-1.5",
                      interactive && "cursor-cell",
                      selected ? "bg-accent-2/35" : "bg-elev-2/60",
                      pending && !changed && "outline-dashed outline-1 outline-warn",
                      changed && "outline outline-2 outline-accent-2",
                    )}
                    onMouseDown={
                      interactive
                        ? (event) => {
                            event.preventDefault();
                            // Focus must not scroll: a scroll under the held button makes a
                            // different cell enter the pointer and extend the selection.
                            element.current?.focus({ preventScroll: true });
                            dragging.current = true;
                            onSelect(
                              event.shiftKey && selection
                                ? { anchor: selection.anchor, focus: [r, c] }
                                : { anchor: [r, c], focus: [r, c] },
                            );
                          }
                        : undefined
                    }
                    onMouseEnter={
                      interactive
                        ? (event) => {
                            // A drag extends only while the primary button is really down:
                            // a stale flag (mouseup missed, or the page scrolled under a
                            // click) must never extend a selection.
                            if (!(event.buttons & 1)) {
                              dragging.current = false;
                              return;
                            }
                            if (dragging.current && selection) {
                              onSelect({ anchor: selection.anchor, focus: [r, c] });
                            }
                          }
                        : undefined
                    }
                  >
                    {text}
                    {changed && (
                      <span className="block text-[0.65rem] text-accent-2">
                        {delta === null
                          ? "changed"
                          : `${delta >= 0 ? "+" : ""}${delta.toFixed(1)} %`}
                      </span>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </TableScroll>
  );
}

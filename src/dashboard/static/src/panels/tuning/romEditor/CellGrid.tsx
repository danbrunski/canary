import { useEffect, useRef } from "react";

import { TableScroll } from "../../../components/TableScroll";
import { cssVar } from "../../../lib/chartTheme";
import {
  formatDecoded,
  isArrowKey,
  moveSelection,
  selectionContains,
} from "../../../lib/romEditor";
import { axisBreakpoints, formatCell } from "../../../lib/romLibrary";
import { mapColor } from "../../../lib/romMaps";
import { cn, mapCellClass, mapGridClass } from "../../../styles";
import type { CellSelection } from "../../../types/romEditor";
import type { RomTableValues } from "../../../types/romLibrary";
import {
  CoverageLegend,
  HeatLegend,
  OverlayMarks,
  inkClass,
  overlayLabel,
  overlayState,
  overlayStyle,
  overlayTitle,
  type GridOverlay,
} from "../../cellOverlay";

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
  return axisBreakpoints(found).values.map((value) => formatCell(value ?? null));
}

/** Marks on a value-shaded cell. Each is drawn in the theme's dark ink (`bg`),
 *  which reads on every stop of the value ramp in both themes
 *  (`colorScale.test.ts` measures it), and none is colour alone: selection is a
 *  ring, a pending cell has a dashed outline and a glyph, a changed cell a
 *  solid outline and its delta. The raw count stays on hover (`title`), which
 *  no fill can hide. */
const SELECTED_RING = `inset 0 0 0 2px ${cssVar("bg")}, inset 0 0 0 3px ${cssVar("fg")}`;

/** One table as a grid of cells, decoded with the raw count on hover and
 *  shaded low to high on the shared value ramp (`mapColor`, the one the ROM
 *  Library and the trim surface use).
 *
 *  Three uses. Interactive (`onSelect` given): click selects a cell,
 *  shift-click or drag a rectangle, arrows move and shift+arrows extend,
 *  and cells a pending edit already selects are dashed. Read-only after
 *  view (`base` given): cells that differ from `base` are outlined and carry
 *  their change in percent. Read-only plain: just the values. With `overlay`
 *  the live marks that were ROM Maps' ride on the grid: the inferred cell, the
 *  ECM's cell and coverage shading, which replaces the value shading. */
export function CellGrid({
  table,
  label,
  selection = null,
  onSelect,
  pendingCells,
  base,
  overlay = null,
  large = false,
}: {
  table: RomTableValues;
  label: string;
  selection?: CellSelection | null;
  onSelect?: (selection: CellSelection | null) => void;
  pendingCells?: Set<string>;
  base?: RomTableValues;
  overlay?: GridOverlay | null;
  large?: boolean;
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
  // The ramp spans the table's own range. A table with one value (a scalar,
  // or every cell equal) has no range to show, so it stays unshaded.
  const numbers = table.raw
    .flatMap((row, r) => row.map((_, c) => shown(table, r, c)))
    .filter((value): value is number => value !== null);
  const min = numbers.length ? Math.min(...numbers) : 0;
  const max = numbers.length ? Math.max(...numbers) : 0;
  const shaded = max > min;
  const coverage = overlay?.coverage ?? null;
  const maxCount = coverage ? Math.max(0, ...coverage.flat()) : 0;

  return (
    <div className="space-y-2">
      <TableScroll className="w-fit rounded-lg border border-border">
        <table
          ref={element}
          role={interactive ? "grid" : "table"}
          aria-label={label}
          aria-multiselectable={interactive ? true : undefined}
          tabIndex={interactive ? 0 : undefined}
          className={cn(
            mapGridClass,
            "select-none font-num outline-none focus-visible:ring-1 focus-visible:ring-accent-2",
            large ? "text-xl" : "text-xs",
          )}
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
            Cells in {unit || "value"}; the raw count is on hover.{" "}
            {coverage
              ? "Shading is sample counts from the coverage overlay."
              : shaded
                ? "Shading runs low to high with the value; it is not a safety rating."
                : ""}
          </caption>
          {colLabels && (
            <thead>
              <tr>
                <th scope="col" className="p-1" />
                {colLabels.map((name, c) => (
                  <th key={c} scope="col" className="whitespace-nowrap p-1 text-fg-dim">
                    {name}
                  </th>
                ))}
              </tr>
            </thead>
          )}
          <tbody>
            {table.raw.map((rawRow, r) => (
              <tr key={r}>
                <th scope="row" className="whitespace-nowrap p-1 text-fg-dim">
                  {rowLabels ? (rowLabels[r] ?? r + 1) : r + 1}
                </th>
                {rawRow.map((raw, c) => {
                  const value = shown(table, r, c);
                  const changed = base ? raw !== base.raw[r]?.[c] : false;
                  const delta = base && changed ? deltaPercent(table, base, r, c) : null;
                  const selected = selectionContains(selection, r, c);
                  const pending = pendingCells?.has(`${r}:${c}`) ?? false;
                  const text = table.decoded ? formatDecoded(value) : formatCell(value);
                  const state = overlayState(overlay, r, c);
                  const style = overlayStyle(state, {
                    heat: shaded && value !== null ? mapColor(value, min, max) : null,
                    maxCount,
                  });
                  const filled = style.backgroundColor !== undefined;
                  const ring = [selected ? SELECTED_RING : null, style.boxShadow]
                    .filter(Boolean)
                    .join(", ");
                  // The overlay's outline marks the inferred cells; an edit's
                  // mark yields to it (the cell still carries its glyph and
                  // data attribute).
                  const editOutline = state.nearby
                    ? null
                    : changed
                      ? `3px solid ${cssVar("bg")}`
                      : pending
                        ? `2px dashed ${cssVar("bg")}`
                        : null;
                  return (
                    <td
                      key={c}
                      role={interactive ? "gridcell" : undefined}
                      aria-selected={interactive ? selected : undefined}
                      aria-label={`Row ${r + 1}, column ${c + 1}: ${text}${
                        changed ? " (changed)" : ""
                      }${overlayLabel(state)}`}
                      data-row={r}
                      data-col={c}
                      data-changed={changed || undefined}
                      data-pending={pending || undefined}
                      data-shaded={filled || undefined}
                      data-trace={state.primary ? "nearest" : state.nearby ? "neighbor" : undefined}
                      data-ecm={state.ecmCell ? "cell" : undefined}
                      data-coverage={state.covered ?? undefined}
                      title={`raw ${raw}${base && changed ? ` (was ${base.raw[r]?.[c]})` : ""}${overlayTitle(state)}`}
                      style={{
                        ...style,
                        boxShadow: ring || undefined,
                        ...(filled && editOutline
                          ? { outline: editOutline, outlineOffset: -3 }
                          : {}),
                        ...(selected ? { fontWeight: 700 } : {}),
                      }}
                      className={cn(
                        "relative rounded",
                        mapCellClass(large ? "xl" : "xs"),
                        interactive && "cursor-cell",
                        filled
                          ? inkClass(style.backgroundColor)
                          : selected
                            ? "bg-accent-2/35"
                            : "bg-elev-2/60",
                        // No fill to read against: the theme's marks do.
                        !filled && pending && !changed && "outline-dashed outline-1 outline-warn",
                        !filled && changed && "outline outline-2 outline-accent-2",
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
                      <OverlayMarks state={state} />
                      {pending && (
                        <span aria-hidden="true" className="absolute bottom-0 left-1 text-[10px]">
                          ✎
                        </span>
                      )}
                      {text}
                      {changed && (
                        <span
                          className={cn(
                            "block text-[0.65rem]",
                            filled ? "font-bold" : "text-accent-2",
                          )}
                        >
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
      {coverage ? (
        <CoverageLegend maxCount={maxCount} />
      ) : shaded ? (
        <HeatLegend
          min={min}
          max={max}
          unit={unit || "value"}
          digits={Number.isInteger(min) && Number.isInteger(max) ? 0 : 2}
        />
      ) : null}
    </div>
  );
}

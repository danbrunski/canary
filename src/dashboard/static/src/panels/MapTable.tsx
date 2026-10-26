import { TableScroll } from "../components/TableScroll";
import { mapColor } from "../lib/romMaps";
import type { RomTable } from "../lib/romMaps";
import type { CellPosition, CoverageGrid } from "../lib/romTrace";
import {
  CoverageLegend,
  HeatLegend,
  OverlayMarks,
  inkClass,
  overlayLabel,
  overlayState,
  overlayStyle,
  overlayTitle,
} from "./cellOverlay";

/** One ROM table as a grid (R08.8): values or coverage shading, the inferred
 *  cell (solid/dashed outlines) and the ECM cell (double ring). The ROM
 *  Library draws Fuel Targets with it; the ROM Editor's `CellGrid` carries the
 *  same overlay marks (`cellOverlay`) on an editable grid. */
export function MapTable({
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
      <p className="text-xs text-fg-dim">
        X: {table.x_label}
        {table.x_unit ? ` (${table.x_unit})` : ""} · Y: {table.y_label}
        {table.y_unit ? ` (${table.y_unit})` : ""} · Cells: {table.unit}
      </p>
      {table.status === "tbd" ? (
        <div className="rounded-lg border border-dashed border-border p-6 text-fg-dim">
          {table.reason} No guessed table values or trace are shown.
        </div>
      ) : (
        <>
          {outOfRange && (
            <p role="status">Operating point is outside the verified axes; no cell highlighted.</p>
          )}
          <TableScroll className="rounded-lg border border-border">
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
                      const state = overlayState({ position, ecm, coverage, dwell }, r, c);
                      const style = overlayStyle(state, {
                        heat: mapColor(value, min, max),
                        maxCount,
                      });
                      return (
                        <td
                          key={c}
                          data-trace={
                            state.primary ? "nearest" : state.nearby ? "neighbor" : undefined
                          }
                          data-ecm={state.ecmCell ? "cell" : undefined}
                          data-coverage={state.covered ?? undefined}
                          aria-label={`${table.y_axis[r]} RPM, ${table.x_axis[c]} ${table.x_unit}: ${value.toFixed(2)} ${table.unit}${overlayLabel(state)}`}
                          title={`Raw ${table.raw[r][c]} · ${value.toFixed(6)} ${table.unit}${overlayTitle(state)}`}
                          className={`relative rounded ${inkClass(style.backgroundColor)} ${large ? "min-w-24 p-5" : "min-w-16 p-3"}`}
                          style={style}
                        >
                          <OverlayMarks state={state} />
                          {value.toFixed(2)}
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
          ) : (
            <HeatLegend min={min} max={max} unit={table.unit} />
          )}
        </>
      )}
    </div>
  );
}

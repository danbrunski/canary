import { cssVar } from "../lib/chartTheme";
import { TableScroll } from "../components/TableScroll";
import { mapColor } from "../lib/romMaps";
import type { RomTable } from "../lib/romMaps";
import { coverageClass, coverageColor, THIN_COVERAGE_SAMPLES } from "../lib/romTrace";
import type { CellPosition, CoverageGrid } from "../lib/romTrace";

/** One ROM table as a grid (R08.8, moved out of RomMapsPanel): values or
 *  coverage shading, the inferred cell (solid/dashed outlines) and the ECM
 *  cell (double ring). */
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
                          className={`relative rounded text-bg ${large ? "min-w-24 p-5" : "min-w-16 p-3"}`}
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
                              ? `${primary ? 3 : 2}px ${primary ? "solid" : "dashed"} ${cssVar("bg")}`
                              : undefined,
                            outlineOffset: -3,
                            boxShadow: ecmCell
                              ? `inset 0 0 0 4px ${cssVar("series-5")}, inset 0 0 0 6px ${cssVar("fg")}`
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
          </TableScroll>
          {coverage ? (
            <div className="flex flex-wrap items-center gap-3 text-xs text-fg-dim">
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
            <div className="flex items-center gap-3 text-xs text-fg-dim">
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

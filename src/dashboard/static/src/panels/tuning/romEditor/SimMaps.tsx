import { memo, useMemo, useState } from "react";

import { SegmentedControl } from "../../../components/SegmentedControl";
import { TableScroll } from "../../../components/TableScroll";
import {
  CHANGED_ZERO_LABEL,
  CHANGED_ZERO_MEANING,
  LOOP_ABBREVIATIONS,
  MARK_LABELS,
  MARK_MEANING,
  MARK_WORDS,
  VIEWS,
  cellLines,
  describePick,
  extentOf,
  g,
  hasChangedZero,
  hatchStyle,
  loopsText,
  mapsForView,
  marksInUse,
  markStyle,
  movedAxes,
  outputName,
  pct,
  quantileLines,
  rampColour,
  rampGradient,
  sharesText,
  supportLines,
  tick,
  weightUnit,
  type MapSpec,
  type Mark,
  type Pick,
  type ViewId,
} from "../../../lib/romSimView";
import type { SimCell, SimCellTable } from "../../../types/romEditor";

const INK = "#111827";
/** Black outline with a white inner line: visible on any fill. */
const EDITED_OUTLINE = "inset 0 0 0 2px #111111, inset 0 0 0 3.5px #ffffff";

interface CellKey {
  row: number;
  col: number;
}

function Swatch({ mark, scale, extent }: { mark: Mark; scale: MapSpec["scale"]; extent: number }) {
  const fill =
    mark === "changed" || mark === "unchanged"
      ? { backgroundColor: rampColour(scale, mark === "changed" ? extent : 0, extent) }
      : hatchStyle(mark);
  const sample = mark === "changed" ? "+" : mark === "unchanged" ? "0" : MARK_LABELS[mark];
  return (
    <span
      aria-hidden="true"
      className="inline-flex h-5 w-9 shrink-0 items-center justify-center rounded-sm border border-border font-num text-[0.65rem]"
      style={{ ...fill, color: INK }}
    >
      {sample}
    </span>
  );
}

function Legend({
  picks,
  map,
  extent,
  table,
}: {
  picks: readonly Pick[];
  map: MapSpec;
  extent: number;
  table: SimCellTable;
}) {
  const marks = marksInUse(picks);
  const unit = map.unit ? ` ${map.unit}` : "";
  const hasEdited = table.cells.some((cell) => cell.edited !== null);
  return (
    <div
      className="space-y-1.5 text-xs text-fg-dim"
      aria-label={`Legend: ${map.title}`}
      role="group"
    >
      {extent > 0 ? (
        <div className="flex items-center gap-2">
          <span className="font-num">
            {map.scale === "diverging" ? `${(-extent).toPrecision(3)}${unit}` : `0${unit}`}
          </span>
          <span
            aria-hidden="true"
            className="h-3 w-40 rounded-sm border border-border"
            style={{ backgroundImage: rampGradient(map.scale, extent) }}
          />
          <span className="font-num">
            {map.scale === "diverging"
              ? `+${extent.toPrecision(3)}${unit}`
              : `${extent.toPrecision(3)}${unit}`}
          </span>
          {map.scale === "diverging" && <span>(0 is neutral)</span>}
        </div>
      ) : (
        <p>No cell in this map holds a supported value, so nothing is coloured.</p>
      )}
      <ul className="flex flex-wrap gap-x-4 gap-y-1">
        {marks.map((mark) => (
          <li key={mark} data-mark={mark} className="flex items-center gap-1.5">
            <Swatch mark={mark} scale={map.scale} extent={extent} />
            <span>
              <strong>{MARK_WORDS[mark]}</strong>: {MARK_MEANING[mark]}
            </span>
          </li>
        ))}
        {hasChangedZero(picks) && (
          <li data-mark="changed_zero" className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="inline-flex h-5 w-9 shrink-0 items-center justify-center rounded-sm border border-border font-num text-[0.65rem]"
              style={{ backgroundColor: rampColour(map.scale, 0, extent), color: INK }}
            >
              {CHANGED_ZERO_LABEL}
            </span>
            <span>
              <strong>changed, median 0</strong>: {CHANGED_ZERO_MEANING}
            </span>
          </li>
        )}
        {hasEdited && (
          <li data-mark="edited" className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="inline-block h-5 w-9 shrink-0 rounded-sm"
              style={{ boxShadow: EDITED_OUTLINE }}
            />
            <span>
              <strong>edited</strong>: {MARK_MEANING.edited}
            </span>
          </li>
        )}
        {table.overlay.axis_edits.length > 0 && (
          <li data-mark="axis_moved" className="flex items-center gap-1.5">
            <span aria-hidden="true" className="w-9 shrink-0 text-center text-warn">
              ▲
            </span>
            <span>
              <strong>moved breakpoint</strong>: {MARK_MEANING.axis_moved}
            </span>
          </li>
        )}
      </ul>
    </div>
  );
}

const SimMap = memo(function SimMap({
  table,
  map,
  selected,
  onSelect,
}: {
  table: SimCellTable;
  map: MapSpec;
  selected: CellKey | null;
  onSelect: (key: CellKey) => void;
}) {
  const picks = useMemo(() => table.cells.map((cell) => map.pick(cell)), [table, map]);
  const extent = useMemo(() => extentOf(picks), [picks]);
  const moved = useMemo(() => movedAxes(table), [table]);
  const nCols = table.cols.breakpoints.length;
  const nRows = table.rows.breakpoints.length;
  const rowUnit = table.rows.unit ?? "raw";
  const colUnit = table.cols.unit ?? "raw";
  const signedCells = map.scale === "diverging";

  return (
    <figure className="space-y-2" aria-label={map.title}>
      <figcaption className="text-sm font-semibold">
        {map.title}
        <span className="block font-normal text-fg-dim">
          {map.note}
          {map.unit ? ` (${map.unit})` : ""}
        </span>
      </figcaption>
      <TableScroll className="rounded-lg border border-border">
        <table
          aria-label={`Map: ${map.title}`}
          className="w-full border-separate border-spacing-0.5 text-center font-num text-xs"
        >
          <caption className="sr-only">
            Rows: {table.rows.table} ({rowUnit}). Columns: {table.cols.table} ({colUnit}). Select a
            cell for its detail.
          </caption>
          <thead>
            <tr>
              <th scope="col" className="p-1 text-fg-faint">
                {rowUnit} \ {colUnit}
              </th>
              {table.cols.breakpoints.map((bp, c) => {
                const isMoved = moved.has(`cols:${c}`);
                return (
                  <th
                    key={bp + ":" + c}
                    scope="col"
                    className={isMoved ? "p-1 text-warn" : "p-1 text-fg-dim"}
                    title={`column ${c + 1}: ${tick(table.cols.values[c] ?? null, bp)} ${colUnit} (breakpoint ${bp}, raw ${table.cols.raw[c]})${isMoved ? "; this breakpoint moves in the candidate" : ""}`}
                  >
                    {tick(table.cols.values[c] ?? null, bp)}
                    {isMoved && <span aria-label="breakpoint moves"> ▲</span>}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {Array.from({ length: nRows }, (_, r) => {
              const isMoved = moved.has(`rows:${r}`);
              return (
                <tr key={r}>
                  <th
                    scope="row"
                    className={isMoved ? "p-1 text-warn" : "p-1 text-fg-dim"}
                    title={`row ${r + 1}: ${tick(table.rows.values[r] ?? null, table.rows.breakpoints[r] ?? 0)} ${rowUnit} (breakpoint ${table.rows.breakpoints[r]}, raw ${table.rows.raw[r]})${isMoved ? "; this breakpoint moves in the candidate" : ""}`}
                  >
                    {tick(table.rows.values[r] ?? null, table.rows.breakpoints[r] ?? 0)}
                    {isMoved && <span aria-label="breakpoint moves"> ▲</span>}
                  </th>
                  {Array.from({ length: nCols }, (_, c) => {
                    const index = r * nCols + c;
                    const cell = table.cells[index];
                    const pick = picks[index];
                    if (!cell || !pick) return <td key={c} />;
                    const isSelected = selected?.row === r && selected.col === c;
                    const lines = cellLines(pick, cell, signedCells);
                    return (
                      <td key={c} className="p-0">
                        <button
                          type="button"
                          aria-label={describePick(table, cell, pick, map.unit)}
                          aria-pressed={isSelected}
                          data-status={pick.mark}
                          data-edited={cell.edited ? "true" : undefined}
                          data-axis-moved={
                            cell.row_axis_moved || cell.col_axis_moved ? "true" : undefined
                          }
                          className={
                            "relative flex h-10 w-full min-w-12 flex-col items-center justify-center leading-tight outline-none focus-visible:ring-2 focus-visible:ring-accent-2" +
                            (isSelected ? " ring-2 ring-accent-2" : "")
                          }
                          style={{
                            ...markStyle(pick, map.scale, extent),
                            color: INK,
                            textShadow: "0 0 2px #fff, 0 0 2px #fff",
                            boxShadow: cell.edited ? EDITED_OUTLINE : undefined,
                          }}
                          onClick={() => onSelect({ row: r, col: c })}
                        >
                          {lines.map((line, k) => (
                            <span
                              key={k}
                              className={
                                lines.length === 2 && k === 1 ? "text-[0.6rem]" : undefined
                              }
                            >
                              {line}
                            </span>
                          ))}
                          {(cell.row_axis_moved || cell.col_axis_moved) && (
                            <span
                              aria-hidden="true"
                              className="absolute right-0.5 top-0 text-[0.5rem]"
                            >
                              ▲
                            </span>
                          )}
                        </button>
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </TableScroll>
      <Legend picks={picks} map={map} extent={extent} table={table} />
    </figure>
  );
});

function CellDetail({ table, cell }: { table: SimCellTable; cell: SimCell }) {
  const unit = weightUnit(table);
  const rows = table.rows.unit ?? "raw";
  const cols = table.cols.unit ?? "raw";
  return (
    <section
      aria-label="Cell detail"
      className="space-y-3 rounded-lg border border-border p-3 text-sm"
    >
      <h4 className="font-semibold">
        Cell (row {cell.row + 1}, column {cell.col + 1}) ·{" "}
        {tick(cell.row_value, cell.row_breakpoint)} {rows} ·{" "}
        {tick(cell.col_value, cell.col_breakpoint)} {cols}
      </h4>
      {cell.pairs > 0 ? (
        <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
          <dt className="text-fg-dim">points, pairs, weight</dt>
          <dd>
            n {cell.n_points} {cell.n_points === 1 ? "point" : "points"} · {cell.pairs} pairs ·
            weight {g(cell.weight)} {unit} ({pct(cell.weight / table.weight_total)} of the run)
          </dd>
          <dt className="text-fg-dim">regime mix</dt>
          <dd>{sharesText(cell.regime_shares)} (the design&apos;s recorded label of each point)</dd>
          <dt className="text-fg-dim">loops</dt>
          <dd>
            {sharesText(cell.loop_shares)} (the simulated loop of each pair) · {loopsText(cell)}
          </dd>
        </dl>
      ) : (
        <p>No points: the design never visited this cell.</p>
      )}
      {cell.edited && (
        <p>
          Edited: raw {cell.edited.old} → {cell.edited.new}
        </p>
      )}
      {cell.row_axis_moved && <p>Row breakpoint moves ({table.rows.table}).</p>}
      {cell.col_axis_moved && <p>Column breakpoint moves ({table.cols.table}).</p>}

      {cell.pairs > 0 && (
        <TableScroll className="rounded-lg border border-border">
          <table aria-label="Cell outputs" className="w-full min-w-[32rem] text-left">
            <thead className="text-[0.7rem] uppercase tracking-[0.08em] text-fg-faint">
              <tr>
                <th className="px-2 py-1.5">Output</th>
                <th className="px-2 py-1.5">Status</th>
                <th className="px-2 py-1.5">Quantiles, support and reasons</th>
              </tr>
            </thead>
            <tbody>
              {table.outputs.map((name) => {
                const oc = cell.outputs[name];
                return (
                  <tr key={name} className="border-t border-border align-top">
                    <td className="px-2 py-1.5 font-num">{name}</td>
                    <td className="px-2 py-1.5 font-semibold">
                      {oc ? MARK_WORDS[oc.status] : "—"}
                    </td>
                    <td className="space-y-0.5 px-2 py-1.5 text-xs">
                      {oc &&
                        [...quantileLines(oc, name), ...supportLines(oc)].map((line, k) => (
                          <div key={k}>{line}</div>
                        ))}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </TableScroll>
      )}

      {cell.by_loop.length > 0 && (
        <div className="space-y-1">
          <h5 className="font-semibold">By loop (each loop&apos;s headline output)</h5>
          <ul aria-label="Cell loops" className="space-y-1.5">
            {cell.by_loop.map((block) => (
              <li key={block.loop} className="rounded border border-border p-2">
                <div>
                  <strong>{LOOP_ABBREVIATIONS[block.loop] ?? block.loop}</strong> {block.loop}{" "}
                  {pct(block.weight_share)} → {outputName(block.output)} (
                  <span className="font-num">{block.output}</span>):{" "}
                  <strong>{MARK_WORDS[block.cell.status]}</strong>
                </div>
                {[...quantileLines(block.cell, block.output), ...supportLines(block.cell)].map(
                  (line, k) => (
                    <div key={k} className="text-xs">
                      {line}
                    </div>
                  ),
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

/** The maps (W09.4): one view at a time, so only the grid being looked at is
 *  rendered; each is a DOM table of buttons, not the canvas heatmap. Every
 *  status is a label or hatch; only a computed value is a colour. */
export function SimMaps({ table }: { table: SimCellTable }) {
  const [view, setView] = useState<ViewId>("lambda");
  const [selected, setSelected] = useState<CellKey | null>(null);
  const { maps, absent } = useMemo(() => mapsForView(table, view), [table, view]);
  const cell =
    selected === null
      ? null
      : (table.cells[selected.row * table.cols.breakpoints.length + selected.col] ?? null);
  return (
    <section aria-label="Maps" className="space-y-3">
      <h3 className="text-base font-semibold">Maps</h3>
      <p className="text-sm text-fg-dim">
        Rows are {table.rows.table}, columns {table.cols.table}. Colour encodes only a computed
        value; every other state has its own hatch and label. Edited cells are outlined, a
        breakpoint the candidate moves is marked ▲. Select a cell for its detail.
      </p>
      {table.overlay.moved_lookups.length > 0 && (
        <ul aria-label="Lookups the edit moves" className="list-inside list-disc text-sm">
          {table.overlay.moved_lookups.map((lookup) => (
            <li key={lookup.node}>
              Moved lookup <span className="font-num">{lookup.node}</span> (
              {lookup.tables.join(", ")}):{" "}
              {lookup.on_this_grid
                ? "one of its tables is this grid's table or axis"
                : "none of its tables is this grid's table or axis"}
              .
            </li>
          ))}
        </ul>
      )}
      <SegmentedControl label="Map" options={VIEWS} value={view} onChange={setView} />
      {absent.length > 0 && (
        <ul aria-label="Maps not drawn" className="list-inside list-disc text-sm text-fg-dim">
          {absent.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
      {maps.map((map) => (
        <SimMap key={map.key} table={table} map={map} selected={selected} onSelect={setSelected} />
      ))}
      {cell && <CellDetail table={table} cell={cell} />}
    </section>
  );
}

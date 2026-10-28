import { TableScroll } from "../../../components/TableScroll";
import { formatDecoded } from "../../../lib/romEditor";
import { buttonClass } from "../../../styles";
import type { RomEditPreview, RomLevers } from "../../../types/romEditor";
import type { RomTableValues } from "../../../types/romLibrary";
import { CellGrid } from "./CellGrid";

function sameTable(a: RomTableValues, id: string, slot: number | null) {
  return a.id === id && (a.slot ?? null) === slot;
}

/** What this edit can and cannot show: the classifier's `messages`, verbatim
 *  and in order, one per line. The lines are made once, in
 *  `src/analysis/rom_levers.py`, from the ROM map and the comparison of the two
 *  images, and `tools/rom_sim.py inspect` prints the same ones, so nothing is
 *  reworded, grouped, coloured or tabulated here: a string the page wrote
 *  itself is a second answer to the same question.
 *
 *  The block is absent only when `messages` is empty. The classifier never
 *  sends that (its floor is "the edit reaches no output"), so an empty list is
 *  a payload that did not come from it, and the absence is not an all-clear. */
function LeverMessages({ levers }: { levers: RomLevers }) {
  if (levers.messages.length === 0) return null;
  return (
    <section
      aria-label="What this edit can and cannot show"
      className="space-y-1 rounded-lg border border-border p-3"
    >
      <h3 className="text-base font-semibold">What this edit can and cannot show</h3>
      <p className="text-xs text-fg-dim">
        The lines <span className="font-num">rom_sim inspect</span> prints for this edit: for each
        output, whether Simulate (below) can give a number and, if not, what blocks it.
      </p>
      <ul className="space-y-1 text-sm">
        {levers.messages.map((message) => (
          <li key={message} className="break-words">
            {message}
          </li>
        ))}
      </ul>
    </section>
  );
}

/** What a preview returned: what the edit can and cannot show, the after
 *  tables with changed cells outlined and their change in percent, the old ->
 *  new cell list in raw and decoded, what the change reaches, and the spec
 *  download. No candidate image is offered or linked anywhere: only its hash,
 *  and the spec that reproduces it. */
export function PreviewResult({
  preview,
  baseTables,
  onDownload,
}: {
  preview: RomEditPreview;
  baseTables: RomTableValues[];
  onDownload: () => void;
}) {
  const { provenance } = preview;
  const effect = provenance.effect;
  const decoded = (table: RomTableValues | undefined, row: number, col: number) =>
    table?.decoded ? formatDecoded(table.decoded[row]?.[col] ?? null) : null;
  const changedBytes =
    effect === null
      ? ""
      : `, in ${effect.changed_bytes} ${effect.changed_bytes === 1 ? "byte" : "bytes"}`;
  return (
    <div className="space-y-4" aria-label="Preview result" role="region">
      <p className="text-sm text-fg-dim">
        {provenance.cells.length} {provenance.cells.length === 1 ? "cell" : "cells"} changed
        {changedBytes}. Spec <span className="font-num">{preview.spec_sha256.slice(0, 12)}</span>,
        candidate <span className="font-num">{preview.candidate_sha256.slice(0, 12)}</span>. The
        candidate is not flashable and stays in the simulator: there is no image to download. Only
        the spec is offered.
      </p>

      {preview.levers && <LeverMessages levers={preview.levers} />}

      {preview.tables.map((table) => {
        const base = baseTables.find((item) => sameTable(item, table.id ?? "", table.slot ?? null));
        return (
          <section
            key={`${table.id}-${table.slot}`}
            aria-label={`After: ${table.label ?? table.name}`}
            className="space-y-2"
          >
            <h3 className="text-base font-semibold">After: {table.label ?? table.name}</h3>
            <CellGrid
              table={table}
              base={base}
              label={`${table.label ?? table.name} after the edits`}
            />
          </section>
        );
      })}

      <section aria-label="Changed cells" className="space-y-2">
        <h3 className="text-base font-semibold">Changed cells</h3>
        <TableScroll className="rounded-lg border border-border">
          <table className="w-full min-w-[32rem] text-left text-sm" aria-label="Changed cells list">
            <thead className="text-[0.7rem] uppercase tracking-[0.08em] text-fg-faint">
              <tr>
                <th className="px-3 py-2">Table</th>
                <th className="px-3 py-2">Cell</th>
                <th className="px-3 py-2">Raw</th>
                <th className="px-3 py-2">Decoded</th>
              </tr>
            </thead>
            <tbody>
              {provenance.cells.map((cell) => {
                const base = baseTables.find((item) => sameTable(item, cell.table, cell.slot));
                const after = preview.tables.find((item) => sameTable(item, cell.table, cell.slot));
                const from = decoded(base, cell.row, cell.col);
                const to = decoded(after, cell.row, cell.col);
                return (
                  <tr
                    key={`${cell.table}-${cell.row}-${cell.col}`}
                    className="border-t border-border font-num"
                  >
                    <td className="px-3 py-1.5">{base?.name ?? cell.table}</td>
                    <td className="px-3 py-1.5">
                      row {cell.row + 1}, col {cell.col + 1}
                    </td>
                    <td className="px-3 py-1.5">
                      {cell.old} → {cell.new}
                    </td>
                    <td className="px-3 py-1.5">
                      {from !== null && to !== null ? `${from} → ${to}` : "no decode"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </TableScroll>
      </section>

      <section aria-label="Reached outputs" className="space-y-1">
        <h3 className="text-base font-semibold">What it reaches</h3>
        {effect === null ? (
          <p className="text-sm text-fg-dim">
            Not traced: this vehicle has no simulator, so nothing says what the changed bytes reach.
            That is not the same as reaching nothing.
          </p>
        ) : effect.changed_nodes.length === 0 ? (
          <p className="text-sm text-fg-dim">
            The change feeds no node of the application model ({effect.outcome}).
          </p>
        ) : (
          <ul className="list-inside list-disc text-sm">
            {effect.changed_nodes.map((node) => (
              <li key={node} className="font-num">
                {node}
              </li>
            ))}
          </ul>
        )}
        {effect !== null && effect.moved_lookups.length > 0 && (
          <p className="text-xs text-fg-dim">
            {effect.moved_lookups.length}{" "}
            {effect.moved_lookups.length === 1 ? "lookup is" : "lookups are"} read at a moved
            operating point.
          </p>
        )}
      </section>

      {preview.spec_yaml === null ? (
        <p role="note" className="text-sm text-fg-dim">
          No spec to download: {preview.export.tables.join(", ")}{" "}
          {preview.export.tables.length === 1 ? "is" : "are"} not verified in the ROM map, and an
          edit to an unverified table can be previewed but never exported.
        </p>
      ) : (
        <button type="button" className={buttonClass} onClick={onDownload}>
          Download spec
        </button>
      )}
    </div>
  );
}

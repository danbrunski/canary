import { knownAnswersLine, pedalFlagBit0Clear, wotColumnCaveat } from "../../../lib/romSim";
import { g, groupAssumptions, pct, weightUnit } from "../../../lib/romSimView";
import type { RomSimResult, SimRunContext } from "../../../types/romEditor";

/** The prefix of the plant's qualification reason (`fuel_plant.plant_qualification`). */
const PLANT_NOT_QUALIFIED = "plant_not_qualified";
const STRUCTURAL = "structural_error_not_sampled";
const PARAMETER_ONLY = "parameter_uncertainty_only";

function Code({ children }: { children: string }) {
  return <code className="break-all font-num text-xs">{children}</code>;
}

function List({ label, items }: { label: string; items: readonly string[] }) {
  return (
    <div className="space-y-1">
      <h4 className="text-sm font-semibold">
        {label} ({items.length})
      </h4>
      {items.length === 0 ? (
        <p className="text-sm text-fg-dim">none</p>
      ) : (
        <ul className="list-inside list-disc space-y-0.5 text-sm">
          {items.map((item, index) => (
            <li key={`${index}-${item}`}>
              <Code>{item}</Code>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** The caveat block (W09 "Caveats come first"): what the plant is not, what
 *  the spread does not contain, every assumption and unresolved dependency,
 *  what was run on what, and how much of the design was covered. It renders
 *  before the first map, always, and the maps never render without it. */
export function SimCaveats({ result, run }: { result: RomSimResult; run: SimRunContext }) {
  const table = result.cells;
  const qualification = result.caveats.find((item) => item.startsWith(PLANT_NOT_QUALIFIED));
  const why = qualification?.includes(":")
    ? qualification.slice(qualification.indexOf(":") + 1).trim()
    : qualification;
  // `caveats` repeats every assumption (E08.10): the assumption lists below show
  // them, so the caveat list does not, and a string never shows twice.
  const assumptions = groupAssumptions(result.assumptions);
  const shown = new Set([STRUCTURAL, PARAMETER_ONLY, qualification, ...result.assumptions]);
  const others = [...new Set(result.caveats.filter((item) => !shown.has(item)))];
  const visited = table.cells.filter((cell) => cell.pairs > 0).length;
  const placed = table.cells.reduce((sum, cell) => sum + cell.weight, 0);
  const unit = weightUnit(table);
  const provenance = result.provenance;
  const coverage = Object.entries(result.coverage).sort(([a], [b]) => a.localeCompare(b));
  const unplaced = Object.entries(table.unplaced).sort(([a], [b]) => a.localeCompare(b));
  const moved = table.overlay.moved_lookups;
  const wotFlag = run.wotColumnEdited ? pedalFlagBit0Clear(String(run.request.pedal_flag)) : null;

  return (
    <section
      aria-label="Read this first"
      className="space-y-3 rounded-xl border border-warn/70 bg-warn/10 p-4"
    >
      <h3 className="text-base font-semibold">Read this first</h3>
      <ul className="space-y-1.5 text-sm">
        {qualification ? (
          <li>
            <strong>The plant is not qualified.</strong> {why} (<Code>{qualification}</Code>).
            Nothing below is a qualified prediction.
          </li>
        ) : (
          <li>
            <strong>Plant qualification:</strong> the run states no{" "}
            <Code>{PLANT_NOT_QUALIFIED}</Code> caveat.
          </li>
        )}
        <li>
          <strong>Structural error is not sampled</strong> (<Code>{STRUCTURAL}</Code>). The
          plant&apos;s own model error is real and large; it is not a parameter, so no band below
          contains it.
        </li>
        <li>
          <strong>Parameter uncertainty only</strong> (<Code>{PARAMETER_ONLY}</Code>). The spread is
          over the declared parameter brackets and the drawn fill bytes; base and candidate share
          every draw, so the deltas are the product, not absolute values.
        </li>
        {wotFlag !== null && (
          <li>
            <strong>{wotColumnCaveat(wotFlag)}</strong>
          </li>
        )}
        <li>
          <strong>Known answers.</strong> {knownAnswersLine()}
        </li>
      </ul>

      <List label="Every other caveat the run states" items={others} />
      <List label="Plant working assumptions the numbers rest on" items={assumptions.plant} />
      <List label="The design's own assumptions" items={assumptions.design} />
      {assumptions.other.length > 0 && (
        <List label="Other assumptions the result states" items={assumptions.other} />
      )}
      <List
        label="Unresolved: what the result rests on but does not know"
        items={result.unresolved}
      />

      <div className="space-y-1">
        <h4 className="text-sm font-semibold">Images</h4>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="text-fg-dim">base</dt>
          <dd>
            {run.base.label}
            {run.base.revision ? ` (${run.base.revision})` : ""}
            <br />
            <Code>{table.base_image_sha256}</Code>
          </dd>
          <dt className="text-fg-dim">candidate</dt>
          <dd>
            the edited image, in the simulator only: not stored, not flashable
            <br />
            <Code>{table.candidate_image_sha256}</Code>
          </dd>
          <dt className="text-fg-dim">compare_roms outcome</dt>
          <dd>
            <Code>{table.rom_outcome}</Code>
          </dd>
        </dl>
      </div>

      <div className="space-y-1">
        <h4 className="text-sm font-semibold">Edits</h4>
        <p className="text-sm">
          <span className="font-semibold">{run.request.label}</span>:{" "}
          {result.candidate.cells.length} {result.candidate.cells.length === 1 ? "cell" : "cells"}{" "}
          changed in {result.candidate.effect.changed_bytes}{" "}
          {result.candidate.effect.changed_bytes === 1 ? "byte" : "bytes"}, reaching{" "}
          {result.candidate.effect.changed_nodes.length > 0
            ? result.candidate.effect.changed_nodes.join(", ")
            : "no node"}
          .
        </p>
        <ol aria-label="Edit list" className="list-inside list-decimal space-y-0.5 text-sm">
          {run.edits.map((edit, index) => (
            <li key={`${index}-${edit.summary}`}>
              {edit.tableName}: {edit.summary}
              {edit.chain && <span className="font-num text-xs text-fg-dim"> ({edit.chain})</span>}
            </li>
          ))}
        </ol>
        <p className="text-sm text-fg-dim">
          {table.overlay.edited_cells.length} edited{" "}
          {table.overlay.edited_cells.length === 1 ? "cell" : "cells"} of <Code>{table.table}</Code>
          {table.slot === null ? "" : ` slot ${table.slot}`}, outlined on every map.
          {table.overlay.axis_edits.length > 0 &&
            ` ${table.overlay.axis_edits.length} breakpoint(s) move, marked ▲.`}
          {table.overlay.other_edits.map(
            (other) => ` ${other.cells} cell(s) of ${other.table}, off this grid.`,
          )}
        </p>
        {moved.length > 0 && (
          <ul aria-label="Moved lookups" className="list-inside list-disc text-sm">
            {moved.map((lookup) => (
              <li key={lookup.node}>
                moved lookup <Code>{lookup.node}</Code> ({lookup.tables.join(", ")}),{" "}
                {lookup.on_this_grid ? "on this grid" : "off this grid"}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="space-y-1">
        <h4 className="text-sm font-semibold">Design coverage, draws and seed</h4>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="text-fg-dim">design</dt>
          <dd>
            {result.design.points} points on a {table.grid} grid (<Code>{table.design_kind}</Code>),{" "}
            {visited} of {table.cells.length} cells visited
          </dd>
          <dt className="text-fg-dim">weight placed</dt>
          <dd>
            {g(placed)} of {g(table.weight_total)} {unit} ({pct(placed / table.weight_total)})
          </dd>
          <dt className="text-fg-dim">unplaced</dt>
          <dd>
            {unplaced.length === 0
              ? "none"
              : unplaced
                  .map(([why, item]) => `${why}: ${item.pairs} pairs, ${g(item.weight)} ${unit}`)
                  .join("; ")}
          </dd>
          <dt className="text-fg-dim">supported share of weight</dt>
          <dd>
            {coverage.length === 0
              ? "not stated"
              : coverage.map(([name, share]) => `${name} ${pct(share)}`).join(", ")}
          </dd>
          <dt className="text-fg-dim">draws</dt>
          <dd className="font-num">{table.draws}</dd>
          <dt className="text-fg-dim">seed</dt>
          <dd className="font-num">
            {provenance.seed === undefined ? "not stated" : provenance.seed}
          </dd>
          <dt className="text-fg-dim">candidate events</dt>
          <dd>
            {provenance.candidate_events === undefined
              ? "not stated"
              : provenance.candidate_events.length === 0
                ? "none: the same learned state on both"
                : provenance.candidate_events.join(", ")}
          </dd>
        </dl>
      </div>
    </section>
  );
}

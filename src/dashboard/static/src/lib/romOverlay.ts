/**
 * The ROM Editor's live overlay: which library tables can carry it, and the
 * table as the trace code reads it.
 *
 * The overlay was ROM Maps (W05): an inferred operating-point cell, the ECM's
 * own cell from its trace channels, and log coverage. That page decoded an
 * uploaded `.OsirisROM`; the editor works on library images, whose cells and
 * axes come from `/tables` and whose trace definitions come from
 * `/trace` (the vehicle's `rom_viewer.yaml`). A library table is matched to its
 * trace definition by `kind` (the identity the viewer, the cell-residency
 * route and the library already share) and by slot: the trace applies to the
 * slot the target definition is written for, never to a copy in another slot.
 *
 * Nothing is placed on a table the lookup cannot be trusted on. The inferred
 * lookup compares the live BFS (ms) and RPM with the table's displayed axes, so
 * the table's axes must be labelled in those units; an axis that is raw counts
 * would be compared with ms and put the cell in the wrong place, which is a
 * refusal that names the units, not a guess.
 */
import type { RomTableValues } from "../types/romLibrary";
import type { RomTable, RomTraceContext, RomTraceTableInfo } from "../types/workbench";
import { axisBreakpoints } from "./romLibrary";

/** The trace definition a library table is matched to, or null when the
 *  vehicle maps no trace channels to this kind in this slot. */
export function traceInfoFor(
  table: RomTableValues,
  context: RomTraceContext,
): RomTraceTableInfo | null {
  if (table.slot === null || table.slot !== context.map_slot || !table.kind) return null;
  const info = context.tables.find((candidate) => candidate.kind === table.kind);
  return info?.trace ? info : null;
}

export type OverlayTable = { table: RomTable } | { refusal: string };

const complete = (values: (number | null)[]): values is number[] =>
  values.every((value) => value !== null);

const unitName = (unit: string) => (unit ? unit : "raw counts");

/** `table` as `RomTable` (what `tracePosition`, `ecmPosition` and the coverage
 *  overlay read), or the named reason it cannot be. Null when the overlay does
 *  not apply to this table at all (see {@link traceInfoFor}). */
export function overlayTableOf(
  table: RomTableValues,
  context: RomTraceContext,
): OverlayTable | null {
  const info = traceInfoFor(table, context);
  if (!info) return null;
  const rows = table.axes.rows;
  const cols = table.axes.cols;
  if (!rows || !cols || !table.decoded)
    return {
      refusal: `${table.name} has no decoded values or no axes in this map definition, so there is no cell to place.`,
    };
  const x = axisBreakpoints(cols);
  const y = axisBreakpoints(rows);
  if (x.unit !== info.x_unit || y.unit !== info.y_unit)
    return {
      refusal: `The live lookup needs ${info.x_label} in ${unitName(info.x_unit)} on the columns and ${info.y_label} in ${unitName(info.y_unit)} on the rows; this map labels them ${unitName(x.unit)} and ${unitName(y.unit)}, so no cell is placed.`,
    };
  const values = table.decoded;
  if (!complete(x.values) || !complete(y.values) || !values.every(complete))
    return { refusal: `${table.name} has an axis or a cell with no value, so no cell is placed.` };
  return {
    table: {
      slot: table.slot ?? 0,
      kind: info.kind,
      title: table.name,
      x_label: info.x_label,
      x_unit: info.x_unit,
      y_label: info.y_label,
      y_unit: info.y_unit,
      unit: table.unit ?? info.unit,
      status: "available",
      reason: null,
      x_axis: x.values,
      y_axis: y.values,
      // The ROM's axis bytes, which a `raw` trace axis lands on.
      x_axis_raw: cols.raw,
      y_axis_raw: rows.raw,
      values,
      raw: table.raw,
      trace: info.trace,
      editor_disagreement: table.editor_disagreement ?? null,
    },
  };
}

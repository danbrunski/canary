// Pure helpers for the ROM Editor page (W09.3): inverting an engineering
// value to a raw count, turning a cell selection and an operation into an
// E08.1 `Edit`, selection geometry, and the browser-side refusals. Nothing
// here is the authority: the server re-validates every edit with E08.1
// (`rom_edit_spec`), and these checks exist so an edit that cannot be valid
// is stopped before it is sent, under the same reason names.
//
// What this makes impossible rather than documented:
// - a scale has no default rounding (`rounding_undeclared`);
// - a raw outside the cell's dtype is refused (`value_out_of_range`), never
//   clamped;
// - an edit that changes no selected cell is refused (`no_op_edit`);
// - only the fuel chain and the ignition timing trims in Map 1 are offered
//   (`table_not_editable`, `slot_not_simulated`);
// - a masked cell (the trims' flag bit 7, decode mask 0x7F) is edited on its
//   masked value and keeps its other bits, as `rom_edit_spec` does: a result
//   outside 0..mask is `value_out_of_range`, never carried into the flag, and
//   an absolute value that would change a flag bit is `masked_bits_changed`.

import { HttpError } from "../api/http";
import type {
  BuildResult,
  CellIndex,
  CellSelection,
  PendingEdit,
  PlannedCell,
  RomEdit,
  RomEditOperation,
  RomEditRefusal,
  RomEditorDecode,
  SelectionRange,
} from "../types/romEditor";
import type { RomTableGroup, RomTableValues } from "../types/romLibrary";

/** `rom_editor.EDITABLE_TABLES` and `SIM_SLOT`: the fuel chain and the
 *  ignition timing trims (E09.7), Map 1. */
export const EDITABLE_TABLES = [
  "fuel_targets",
  "fuel_compensation",
  "k_constant",
  "timing_trim_a",
  "timing_trim_b",
] as const;
export const SIM_SLOT = 1;

/** The largest raw each dtype stores. */
export const DTYPE_MAX: Record<string, number> = { u8: 0xff, be16: 0xffff };

/** Whether the page offers this table for editing. */
export function isEditableTable(table: RomTableValues): boolean {
  return (
    table.role !== "axis" &&
    table.slot === SIM_SLOT &&
    table.id !== undefined &&
    (EDITABLE_TABLES as readonly string[]).includes(table.id)
  );
}

/** Whether the page lists this table: every table the map declares except
 *  the axes, curves and scalars included (operator, 2026-10-27). A curve is a
 *  one-row or one-column grid, a scalar one cell. Showing a table is not
 *  editing it: only `isEditableTable` ones take an edit. */
export function isViewableTable(table: RomTableValues): boolean {
  return table.role !== "axis";
}

/** The table tree's label: the map's short name, else its name (W10.7). */
export function treeLabel(table: RomTableValues): string {
  return table.short_name ?? table.name;
}

export type TreeGroupId = RomTableGroup | "other";

/** The tree's groups in order: `rom_map.py`'s closed set, then Other for a
 *  table that carries none (a daemon older than W10.7). */
export const TREE_GROUPS: ReadonlyArray<{ id: TreeGroupId; label: string }> = [
  { id: "fuel", label: "Fuel" },
  { id: "ignition", label: "Ignition" },
  { id: "airflow", label: "Airflow" },
  { id: "emissions", label: "Emissions" },
  { id: "other", label: "Other" },
];

function treeGroupOf(table: RomTableValues): TreeGroupId {
  return TREE_GROUPS.find((group) => group.id === table.group)?.id ?? "other";
}

/** Whether `filter` (any case) is in the table's tree label or its name. */
export function matchesTableFilter(table: RomTableValues, filter: string): boolean {
  const needle = filter.trim().toLowerCase();
  if (!needle) return true;
  return [treeLabel(table), table.name].some((text) => text.toLowerCase().includes(needle));
}

/** The tree's Editable / View only / All choice (W10.15). */
export type TableKinds = "all" | "editable" | "view";

export const TABLE_KINDS: readonly TableKinds[] = ["all", "editable", "view"];

function matchesKinds(table: RomTableValues, kinds: TableKinds): boolean {
  if (kinds === "all") return true;
  return isEditableTable(table) === (kinds === "editable");
}

/** One line of the tree: a table with a single copy, or a family of slotted
 *  copies sharing a tree label (Fuel Targets in Maps 1-5), in slot order. */
export type TreeEntry =
  | { kind: "table"; table: RomTableValues }
  | { kind: "family"; key: string; label: string; tables: RomTableValues[] };

/** The family a slotted copy belongs to (its tree label); null for a table
 *  with one copy. `rom_map.py` refuses two tables with one label in a slot,
 *  so a label names one family. */
export function familyKey(table: RomTableValues): string | null {
  return table.slot === null ? null : treeLabel(table);
}

/** The tree (W10.14): by group in `TREE_GROUPS` order, the daemon's order
 *  within each, every slotted copy folded into its family where the first
 *  copy stands. Only what `filter` and `kinds` keep is listed; a family left
 *  with one copy is listed as that copy, and empty groups are dropped. */
export function treeGroups(
  tables: readonly RomTableValues[],
  filter = "",
  kinds: TableKinds = "all",
): Array<{ id: TreeGroupId; label: string; entries: TreeEntry[] }> {
  const kept = tables.filter(
    (table) => matchesTableFilter(table, filter) && matchesKinds(table, kinds),
  );
  return TREE_GROUPS.map((group) => {
    const entries: TreeEntry[] = [];
    const families = new Map<string, RomTableValues[]>();
    for (const table of kept.filter((item) => treeGroupOf(item) === group.id)) {
      const key = familyKey(table);
      if (key === null) {
        entries.push({ kind: "table", table });
        continue;
      }
      const copies = families.get(key);
      if (copies) {
        copies.push(table);
        continue;
      }
      const family: RomTableValues[] = [table];
      families.set(key, family);
      entries.push({ kind: "family", key, label: key, tables: family });
    }
    return {
      ...group,
      entries: entries.map((entry): TreeEntry => {
        if (entry.kind === "table") return entry;
        const [only] = entry.tables;
        if (entry.tables.length === 1 && only) return { kind: "table", table: only };
        return {
          ...entry,
          tables: [...entry.tables].sort((a, b) => (a.slot ?? 0) - (b.slot ?? 0)),
        };
      }),
    };
  }).filter((group) => group.entries.length > 0);
}

/** How many pending edits change this table (its id is unique per file, so a
 *  Map 1 table is never confused with its copy in Maps 2-5). */
export function pendingEditCount(pending: readonly PendingEdit[], table: RomTableValues): number {
  return table.id === undefined ? 0 : pending.filter((item) => item.edit.table === table.id).length;
}

/** One table of one slot, for picking between them: a table id is shared by
 *  the copies in Maps 2-5, so the slot is part of the identity. */
export function tableChoice(table: RomTableValues): string {
  return `${table.slot ?? "-"}:${table.id ?? table.name}`;
}

// -- decode ---------------------------------------------------------------------------------

interface DecodeParams {
  kind: "linear" | "reciprocal";
  scale: number;
  offset: number;
  numerator: number;
  mask: number | null;
}

const NUMBER = String.raw`(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)`;

/** The decode's numbers: the declared parameters when the route sends them,
 *  else the ones `Decode.formula` prints (`afr = 1881.6 / raw`,
 *  `fuel compensation = raw × 0.78125 + 3`). Null when neither gives them. */
export function decodeParams(decode: RomEditorDecode): DecodeParams | null {
  const mask =
    decode.mask ??
    (() => {
      const found = /\(raw & 0x([0-9a-f]+)\)/i.exec(decode.formula ?? "");
      return found?.[1] ? parseInt(found[1], 16) : null;
    })();
  if (decode.kind === "reciprocal") {
    let numerator = decode.numerator ?? null;
    if (numerator === null) {
      const found = new RegExp(String.raw`=\s*${NUMBER}\s*/`, "i").exec(decode.formula ?? "");
      numerator = found?.[1] ? Number(found[1]) : null;
    }
    return numerator === null || !Number.isFinite(numerator) || numerator === 0
      ? null
      : { kind: "reciprocal", scale: 0, offset: 0, numerator, mask };
  }
  if (decode.kind === "linear") {
    let scale = decode.scale ?? null;
    let offset = decode.offset ?? null;
    if (scale === null) {
      const found = new RegExp(String.raw`×\s*${NUMBER}(?:\s*\+\s*${NUMBER})?`, "i").exec(
        decode.formula ?? "",
      );
      scale = found?.[1] ? Number(found[1]) : null;
      offset = found?.[2] ? Number(found[2]) : 0;
    }
    return scale === null || !Number.isFinite(scale) || scale === 0
      ? null
      : { kind: "linear", scale, offset: offset ?? 0, numerator: 0, mask };
  }
  return null;
}

/** Round half up after dropping float noise (1.02 x 100 is 102, not
 *  102.00000000000001, and 2.5 rounds to 3). */
function roundHalfUp(value: number): number {
  return Math.floor(Number(value.toPrecision(12)) + 0.5);
}

/** The engineering value of `raw` under `decode` (`Decode.apply`); null for
 *  a raw 0 under a reciprocal, or when the decode's numbers are unknown. */
export function valueForRaw(
  decode: RomEditorDecode | null | undefined,
  raw: number,
): number | null {
  if (!decode) return null;
  const params = decodeParams(decode);
  if (!params) return null;
  const cell = params.mask === null ? raw : raw & params.mask;
  if (params.kind === "linear") return cell * params.scale + params.offset;
  return cell === 0 ? null : params.numerator / cell;
}

/** The raw count whose decoded value is nearest `value`: linear
 *  `(v − offset) / scale`, reciprocal `numerator / v`, rounded to the nearest
 *  integer. Not clamped: the caller checks the cell's dtype. Null when no
 *  raw exists (non-finite value, a reciprocal at 0, a masked cell whose flag
 *  bit the decode strips, or a decode whose numbers are unknown). */
export function rawForValue(
  decode: RomEditorDecode | null | undefined,
  value: number,
): number | null {
  if (!decode || !Number.isFinite(value)) return null;
  const params = decodeParams(decode);
  if (!params || params.mask !== null) return null;
  return invert(params, value);
}

function invert(params: DecodeParams, value: number): number | null {
  if (params.kind === "linear") return roundHalfUp((value - params.offset) / params.scale);
  if (value === 0) return null;
  return roundHalfUp(params.numerator / value);
}

/** The cell's value bits for `value` under a masked decode (`raw & mask`
 *  decodes to `value`), flag bits left to the cell. Null as `rawForValue`, and
 *  for an unmasked decode (use `rawForValue`). */
export function maskedRawForValue(
  decode: RomEditorDecode | null | undefined,
  value: number,
): number | null {
  if (!decode || !Number.isFinite(value)) return null;
  const params = decodeParams(decode);
  if (!params || params.mask === null) return null;
  return invert(params, value);
}

/** The decode's value mask, or null for an unmasked (or unreadable) decode. */
export function valueMask(decode: RomEditorDecode | null | undefined): number | null {
  if (!decode) return null;
  return decodeParams(decode)?.mask ?? null;
}

const hex = (n: number) => `0x${n.toString(16).toUpperCase()}`;

/** A decoded value for the page: four significant digits (`12.46`). */
export function formatDecoded(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "–";
  return String(Number(value.toPrecision(4)));
}

// -- selection ------------------------------------------------------------------------------

/** The inclusive bounds of a selection, whichever corner is the anchor. */
export function selectionRange(selection: CellSelection): SelectionRange {
  return {
    r0: Math.min(selection.anchor[0], selection.focus[0]),
    r1: Math.max(selection.anchor[0], selection.focus[0]),
    c0: Math.min(selection.anchor[1], selection.focus[1]),
    c1: Math.max(selection.anchor[1], selection.focus[1]),
  };
}

/** Every selected cell, row-major. */
export function selectionCells(selection: CellSelection): CellIndex[] {
  const { r0, r1, c0, c1 } = selectionRange(selection);
  const cells: CellIndex[] = [];
  for (let r = r0; r <= r1; r += 1) for (let c = c0; c <= c1; c += 1) cells.push([r, c]);
  return cells;
}

export function selectionContains(selection: CellSelection | null, row: number, col: number) {
  if (!selection) return false;
  const { r0, r1, c0, c1 } = selectionRange(selection);
  return row >= r0 && row <= r1 && col >= c0 && col <= c1;
}

/** `6 cells (rows 1–2, columns 1–3)`, 1-based as the grid labels them. */
export function selectionText(selection: CellSelection): string {
  const { r0, r1, c0, c1 } = selectionRange(selection);
  const count = (r1 - r0 + 1) * (c1 - c0 + 1);
  const span = (a: number, b: number) => (a === b ? `${a + 1}` : `${a + 1}–${b + 1}`);
  return `${count} ${count === 1 ? "cell" : "cells"} (row ${span(r0, r1)}, column ${span(c0, c1)})`;
}

const ARROWS: Record<string, CellIndex> = {
  ArrowUp: [-1, 0],
  ArrowDown: [1, 0],
  ArrowLeft: [0, -1],
  ArrowRight: [0, 1],
};

export function isArrowKey(key: string): boolean {
  return key in ARROWS;
}

const clamp = (value: number, max: number) => Math.min(Math.max(value, 0), max - 1);

/** An arrow moves the selection to one cell; with shift it extends from the
 *  anchor. With nothing selected, any arrow selects the first cell. */
export function moveSelection(
  selection: CellSelection | null,
  key: string,
  extend: boolean,
  rows: number,
  cols: number,
): CellSelection | null {
  const step = ARROWS[key];
  if (!step || rows < 1 || cols < 1) return selection;
  if (!selection) return { anchor: [0, 0], focus: [0, 0] };
  const focus: CellIndex = [
    clamp(selection.focus[0] + step[0], rows),
    clamp(selection.focus[1] + step[1], cols),
  ];
  return extend ? { anchor: selection.anchor, focus } : { anchor: focus, focus };
}

// -- operations -> edits --------------------------------------------------------------------

function refuse(reason: string, message: string): BuildResult {
  return { ok: false, reason, message };
}

/** `scale` as the edit carries it: 1 + percent / 100 without float noise. */
export function factorForPercent(percent: number): number {
  return Number((1 + percent / 100).toPrecision(12));
}

/** `raw x factor` rounded as declared; the server's exact-decimal rule
 *  (`rom_edit_spec`), to the precision a typed factor can carry. */
function scaledRaw(raw: number, factor: number, rounding: "nearest" | "floor" | "ceil"): number {
  const product = Number((raw * factor).toPrecision(12));
  if (rounding === "floor") return Math.floor(product);
  if (rounding === "ceil") return Math.ceil(product);
  return Math.floor(product + 0.5);
}

/** Turn the operator's selection and operation into one E08.1 `Edit` on the
 *  exact selected `cells`, or refuse. The old and new raw of each cell (and
 *  decoded where a decode exists) come back so the page can show what will
 *  happen before the server is asked. */
export function buildEdits(
  table: RomTableValues,
  selection: CellSelection,
  op: RomEditOperation,
): BuildResult {
  if (!isEditableTable(table) || table.id === undefined) {
    return table.slot !== SIM_SLOT && table.slot !== null
      ? refuse(
          "slot_not_simulated",
          `${table.name} slot ${table.slot}: the simulator runs Map ${SIM_SLOT} only`,
        )
      : refuse("table_not_editable", `${table.name} is not editable here`);
  }
  const rows = table.raw.length;
  const cols = table.raw[0]?.length ?? 0;
  const { r0, r1, c0, c1 } = selectionRange(selection);
  if (r0 < 0 || c0 < 0 || r1 >= rows || c1 >= cols) {
    return refuse(
      "selection_outside_table",
      `the selection is outside the ${rows} × ${cols} table`,
    );
  }
  const decode = table.decode ?? null;
  const max = DTYPE_MAX[table.dtype ?? "u8"] ?? 0xff;
  const cells = selectionCells(selection);
  const mask = valueMask(decode);
  const flagsOf = (raw: number) => (mask === null ? 0 : raw & ~mask & max);
  // The flag bits the selected cells carry, when the table has a value mask.
  const flagSets = new Set(cells.map(([row, col]) => flagsOf(table.raw[row]?.[col] ?? 0)));

  let nextRaw: (old: number) => number;
  let operation: Pick<RomEdit, "set_raw" | "add_raw" | "scale" | "rounding">;
  let chain: string | null = null;
  const arithmeticMasked = (): BuildResult | null =>
    mask !== null && (mask & (mask + 1)) !== 0
      ? refuse(
          "arithmetic_on_masked_cells",
          `${table.name}'s value mask ${hex(mask)} is not contiguous low bits, so arithmetic on it has no meaning`,
        )
      : null;
  /** An absolute value on a masked table must leave every flag bit as it is:
   *  one `set_raw` cannot keep two cells' different flags. */
  const sharedFlags = (): BuildResult | number => {
    if (mask === null || flagSets.size <= 1) return [...flagSets][0] ?? 0;
    // The backend's name for it: `rom_edit_spec` refuses `masked_bits_changed` at the first
    // cell whose flag bits the one `set_raw` would change.
    return refuse(
      "masked_bits_changed",
      `the selected cells carry different flag bits (${[...flagSets].map(hex).join(", ")} outside the value mask ${hex(mask)}), and one absolute value cannot keep both. Select cells that share them, or use Add or Scale, which keep each cell's flag bits.`,
    );
  };
  const absoluteMasked = (raw: number): BuildResult | null => {
    if (mask === null) return null;
    const flags = sharedFlags();
    if (typeof flags !== "number") return flags;
    if ((raw & ~mask) === flags) return null;
    // A raw outside the dtype changes the bits outside the mask too: the backend calls that
    // `masked_bits_changed` as well (it checks the mask before the dtype).
    return refuse(
      "masked_bits_changed",
      `${table.name}'s value mask is ${hex(mask)}: the selected cells hold flag bits ${hex(flags)}, and raw ${raw}${
        raw < 0 || raw > max ? ` (outside a ${table.dtype ?? "u8"} cell's 0..${max})` : ""
      } would change them. Use Set to value, which keeps the flag bits, or include them in the raw count.`,
    );
  };
  switch (op.kind) {
    case "set_value": {
      if (!decode) {
        return refuse(
          "decode_not_declared",
          `${table.name} has no confirmed decode, so it has no engineering value to set; set a raw value`,
        );
      }
      let raw = mask === null ? rawForValue(decode, op.value) : maskedRawForValue(decode, op.value);
      if (raw === null) {
        return refuse(
          "value_not_invertible",
          `no raw count decodes to ${op.value} ${decode.unit} (${decode.formula ?? decode.kind})`,
        );
      }
      if (mask !== null) {
        if (raw < 0 || raw > mask) {
          return refuse(
            "value_out_of_range",
            `${op.value} ${decode.unit} is ${raw}; the value bits of a ${table.dtype ?? "u8"} cell under mask ${hex(mask)} hold 0..${mask}. Nothing is clamped, and nothing is carried into the flag bits.`,
          );
        }
        // The flag bits stay what the selected cells hold: the edit sets the value bits only.
        const flags = sharedFlags();
        if (typeof flags !== "number") return flags;
        raw |= flags;
      } else if (raw < 0 || raw > max) {
        return refuse(
          "value_out_of_range",
          `${op.value} ${decode.unit} is raw ${raw}; a ${table.dtype ?? "u8"} cell holds 0..${max}. Nothing is clamped.`,
        );
      }
      const back = valueForRaw(decode, raw);
      chain = `${formatDecoded(op.value)} ${decode.unit} → raw ${raw}${
        mask !== null && flagsOf(raw) ? ` (flag bits ${hex(flagsOf(raw))} kept)` : ""
      } → ${formatDecoded(back)} ${decode.unit}`;
      const fixed = raw;
      nextRaw = () => fixed;
      operation = { set_raw: raw };
      break;
    }
    case "set_raw": {
      if (!Number.isInteger(op.raw))
        return refuse("not_an_integer", "a raw value is a whole number");
      // An unmasked table's out-of-dtype raw is refused per cell below (`value_out_of_range`);
      // a masked table's is a flag-bit change first (`masked_bits_changed`), as in the backend.
      const refused = absoluteMasked(op.raw);
      if (refused) return refused;
      nextRaw = () => op.raw;
      operation = { set_raw: op.raw };
      break;
    }
    case "add_raw": {
      if (!Number.isInteger(op.delta)) {
        return refuse("not_an_integer", "the raw amount to add is a whole number");
      }
      const refused = arithmeticMasked();
      if (refused) return refused;
      nextRaw = (old) => old + op.delta;
      operation = { add_raw: op.delta };
      break;
    }
    case "scale": {
      if (!Number.isFinite(op.percent))
        return refuse("not_a_number", "enter the scale as a percent");
      const factor = factorForPercent(op.percent);
      if (!(factor > 0))
        return refuse("scale_not_positive", `${op.percent} % leaves a factor of ${factor}`);
      if (op.rounding === null) {
        return refuse(
          "rounding_undeclared",
          "a scale needs rounding: nearest, floor or ceil (no default)",
        );
      }
      const refused = arithmeticMasked();
      if (refused) return refused;
      const rounding = op.rounding;
      nextRaw = (old) => scaledRaw(old, factor, rounding);
      operation = { scale: factor, rounding };
      break;
    }
  }

  const planned: PlannedCell[] = [];
  for (const [row, col] of cells) {
    const oldRaw = table.raw[row]?.[col];
    if (oldRaw === undefined) {
      return refuse(
        "selection_outside_table",
        `row ${row + 1}, column ${col + 1} is not in the table`,
      );
    }
    // A masked table's arithmetic acts on the value bits and keeps the flag bits
    // (`rom_edit_spec`); an absolute value was checked against them above.
    const arithmetic = op.kind === "add_raw" || op.kind === "scale";
    const onValue = mask !== null && arithmetic;
    const next = nextRaw(onValue ? oldRaw & mask : oldRaw);
    const limit = onValue ? mask : max;
    if (next < 0 || next > limit) {
      return refuse(
        "value_out_of_range",
        onValue
          ? `row ${row + 1}, column ${col + 1}: the value bits ${oldRaw & mask} become ${next}, outside 0..${mask} (mask ${hex(mask)}). Nothing is clamped, and nothing is carried into the flag bits.`
          : `row ${row + 1}, column ${col + 1}: raw ${oldRaw} becomes ${next}; a ${table.dtype ?? "u8"} cell holds 0..${max}. Nothing is clamped.`,
      );
    }
    const newRaw = onValue ? (oldRaw & ~mask) | next : next;
    planned.push({
      row,
      col,
      oldRaw,
      newRaw,
      oldDecoded: valueForRaw(decode, oldRaw),
      newDecoded: valueForRaw(decode, newRaw),
    });
  }
  if (planned.every((cell) => cell.newRaw === cell.oldRaw)) {
    return refuse(
      "no_op_edit",
      `this leaves all ${planned.length} selected ${planned.length === 1 ? "cell" : "cells"} at ${
        planned.length === 1 ? "its" : "their"
      } current value`,
    );
  }
  return {
    ok: true,
    edits: [{ table: table.id, slot: table.slot, cells, ...operation }],
    cells: planned,
    chain,
  };
}

/** The pending list's sentence for an applied operation. */
export function describeOperation(
  op: RomEditOperation,
  cells: number,
  unit: string | null,
): string {
  const on = `on ${cells} ${cells === 1 ? "cell" : "cells"}`;
  switch (op.kind) {
    case "set_value":
      return `set to ${op.value}${unit ? ` ${unit}` : ""} ${on}`;
    case "set_raw":
      return `set raw ${op.raw} ${on}`;
    case "add_raw":
      return `add raw ${op.delta >= 0 ? "+" : ""}${op.delta} ${on}`;
    case "scale":
      return `scale raw ×${factorForPercent(op.percent)} (${op.percent >= 0 ? "+" : ""}${op.percent} %, ${op.rounding ?? "no rounding"}) ${on}`;
  }
}

/** `table:row:col` keys of the cells a pending edit selects. */
function cellKeys(edit: RomEdit): string[] {
  return (edit.cells ?? []).map(([row, col]) => `${edit.table}:${edit.slot ?? ""}:${row}:${col}`);
}

/** Cells in `edit` that a pending edit already selects. E08.1 refuses two
 *  edits on one cell (`duplicate_edit`: composition order is not
 *  expressible), so the page says so before sending. */
export function duplicateCells(pending: PendingEdit[], edit: RomEdit): CellIndex[] {
  const taken = new Set(pending.flatMap((item) => cellKeys(item.edit)));
  return (edit.cells ?? []).filter(([row, col]) =>
    taken.has(`${edit.table}:${edit.slot ?? ""}:${row}:${col}`),
  );
}

/** The cells a table's pending edits touch, for outlining them on the grid. */
export function pendingCellSet(pending: PendingEdit[], tableId: string): Set<string> {
  const out = new Set<string>();
  for (const item of pending) {
    if (item.edit.table !== tableId) continue;
    for (const [row, col] of item.edit.cells ?? []) out.add(`${row}:${col}`);
  }
  return out;
}

// -- refusals from the route ----------------------------------------------------------------

function asRefusal(value: unknown): RomEditRefusal | null {
  if (typeof value !== "object" || value === null) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.reason !== "string") return null;
  return {
    reason: record.reason,
    message: typeof record.message === "string" ? record.message : record.reason,
  };
}

/** The server's named refusal from a failed preview: `{error: "edit_refused",
 *  reason, message}` (or the same inside FastAPI's `detail`). Null for any
 *  other failure, which the caller describes as an ordinary error. */
export function previewRefusal(err: unknown): RomEditRefusal | null {
  if (!(err instanceof HttpError) || err.status !== 400) return null;
  let body: unknown;
  try {
    body = JSON.parse(err.body);
  } catch {
    return null;
  }
  if (typeof body !== "object" || body === null) return null;
  const record = body as Record<string, unknown>;
  return asRefusal(record) ?? asRefusal(record.detail);
}

// -- persistence ----------------------------------------------------------------------------

/** What survives a reload, per base image. */
export interface SavedEditorState {
  label: string;
  pending: PendingEdit[];
}

const STORAGE_PREFIX = "canary.romEditor.";

export function storageKey(sha: string): string {
  return `${STORAGE_PREFIX}${sha}`;
}

function isPendingEdit(value: unknown): value is PendingEdit {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  const edit = item.edit as Record<string, unknown> | null | undefined;
  return (
    typeof item.id === "string" &&
    typeof item.summary === "string" &&
    typeof item.tableName === "string" &&
    typeof edit === "object" &&
    edit !== null &&
    typeof edit.table === "string"
  );
}

/** The saved pending edits and label for `sha`; empty when absent, unreadable
 *  or shaped wrongly (`localStorage` may be blocked or hold another version). */
export function loadEditorState(sha: string): SavedEditorState {
  try {
    const text = window.localStorage.getItem(storageKey(sha));
    if (!text) return { label: "", pending: [] };
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed !== "object" || parsed === null) return { label: "", pending: [] };
    const record = parsed as Record<string, unknown>;
    return {
      label: typeof record.label === "string" ? record.label : "",
      pending: Array.isArray(record.pending) ? record.pending.filter(isPendingEdit) : [],
    };
  } catch {
    return { label: "", pending: [] };
  }
}

export function saveEditorState(sha: string, state: SavedEditorState): void {
  try {
    if (!state.label && state.pending.length === 0) window.localStorage.removeItem(storageKey(sha));
    else window.localStorage.setItem(storageKey(sha), JSON.stringify(state));
  } catch {
    // Storage blocked or full: the edits live on in memory for this visit.
  }
}

// -- the spec download ----------------------------------------------------------------------

/** `<label>.edit-spec.yaml`, filesystem-safe. */
export function specFileName(label: string): string {
  const stem = label
    .trim()
    .replace(/[^A-Za-z0-9._-]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return `${stem || "rom-edit"}.edit-spec.yaml`;
}

/** Whether a library entry can be simulated and, if not, the reason. The
 *  rule is `rom_editor.sim_base`: a `matched` revision only. */
export function simulateReason(
  revisionMatch: { status: string; revision: string | null } | null | undefined,
): string | null {
  if (revisionMatch?.status === "matched" && revisionMatch.revision) return null;
  return "base_not_a_recorded_revision";
}

export const SIMULATE_REASON_TEXT: Record<string, string> = {
  base_not_a_recorded_revision:
    "The simulator needs a base recorded in tune_revisions.yaml. This image is not one (a reference image, such as a tuner's base that was never flashed, is not a revision), so edits can be previewed but not simulated.",
};

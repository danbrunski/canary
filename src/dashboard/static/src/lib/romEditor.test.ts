import { afterEach, describe, expect, it } from "vitest";

import { HttpError } from "../api/http";
import { TABLES, TABLES_WITH_FUEL_COMP } from "../test/romLibraryFixtures";
import { timingTrimTable } from "../test/romSimFixtures";
import type { CellSelection, PendingEdit, RomEditorDecode } from "../types/romEditor";
import type { RomTableValues } from "../types/romLibrary";
import {
  buildEdits,
  decodeParams,
  duplicateCells,
  isEditableTable,
  pendingEditCount,
  loadEditorState,
  maskedRawForValue,
  moveSelection,
  previewRefusal,
  rawForValue,
  saveEditorState,
  selectionCells,
  selectionRange,
  selectionText,
  simulateReason,
  specFileName,
  storageKey,
  valueForRaw,
  valueMask,
} from "./romEditor";

const fuelTargets = TABLES.tables[0];
const kConstant = TABLES.tables[1];
const fuelComp = TABLES_WITH_FUEL_COMP.tables.find(
  (table) => table.id === "fuel_compensation",
) as RomTableValues;
const AFR = fuelTargets.decode as RomEditorDecode;
const COMP = fuelComp.decode as RomEditorDecode;

const sel = (a: [number, number], b: [number, number]): CellSelection => ({
  anchor: a,
  focus: b,
});

afterEach(() => window.localStorage.clear());

describe("rawForValue", () => {
  it("inverts the reciprocal Fuel Targets decode: 12.5 AFR -> raw 151 -> 12.46", () => {
    expect(rawForValue(AFR, 12.5)).toBe(151);
    expect(valueForRaw(AFR, 151)).toBeCloseTo(12.4609, 3);
  });

  it("inverts a linear decode and rounds half up", () => {
    // 103 % / 0.78125 = 131.84 -> 132; 0.78125 x 132 = 103.125
    expect(rawForValue(COMP, 103)).toBe(132);
    expect(valueForRaw(COMP, 132)).toBeCloseTo(103.125, 6);
    // Exactly between two counts (100.78125 / 0.78125 = 129.0 + 0.5 count).
    expect(rawForValue(COMP, 0.78125 * 100.5)).toBe(101);
  });

  it("uses the declared parameters over the formula when the route sends them", () => {
    const declared: RomEditorDecode = { ...COMP, scale: 0.5, offset: 10, formula: "ignored" };
    expect(rawForValue(declared, 60)).toBe(100);
    expect(decodeParams({ ...AFR, numerator: 100 })?.numerator).toBe(100);
  });

  it("falls back to the formula's own numbers when the declared ones are absent", () => {
    // An older daemon sends only the formula string.
    const { formula, kind, quantity, unit } = AFR;
    expect(rawForValue({ formula, kind, quantity, unit }, 12.5)).toBe(151);
    const comp = COMP;
    expect(
      rawForValue(
        { formula: comp.formula, kind: comp.kind, quantity: comp.quantity, unit: comp.unit },
        103,
      ),
    ).toBe(132);
  });

  it("reads an offset off the formula", () => {
    const decode: RomEditorDecode = {
      kind: "linear",
      quantity: "x",
      unit: "u",
      formula: "x = raw × 2 + 10",
    };
    expect(rawForValue(decode, 50)).toBe(20);
  });

  it("has no raw where none exists", () => {
    expect(rawForValue(null, 12.5)).toBeNull();
    expect(rawForValue(AFR, 0)).toBeNull();
    expect(rawForValue(AFR, Number.NaN)).toBeNull();
    expect(rawForValue({ kind: "linear", quantity: "x", unit: "u" }, 1)).toBeNull();
    // A masked cell keeps flag bits the decode strips: no value maps to one raw.
    expect(rawForValue({ ...COMP, mask: 0x7f }, 50)).toBeNull();
  });
});

describe("selection", () => {
  it("normalizes either corner order", () => {
    expect(selectionRange(sel([3, 4], [1, 2]))).toEqual({ r0: 1, r1: 3, c0: 2, c1: 4 });
    expect(selectionCells(sel([1, 1], [2, 2]))).toEqual([
      [1, 1],
      [1, 2],
      [2, 1],
      [2, 2],
    ]);
    expect(selectionText(sel([1, 1], [2, 3]))).toBe("6 cells (row 2–3, column 2–4)");
    expect(selectionText(sel([0, 0], [0, 0]))).toBe("1 cell (row 1, column 1)");
  });

  it("moves with arrows, extends with shift, and stays inside the table", () => {
    const start = sel([0, 0], [0, 0]);
    expect(moveSelection(null, "ArrowDown", false, 8, 8)).toEqual(start);
    expect(moveSelection(start, "ArrowUp", false, 8, 8)).toEqual(start);
    expect(moveSelection(start, "ArrowRight", false, 8, 8)).toEqual(sel([0, 1], [0, 1]));
    const extended = moveSelection(start, "ArrowDown", true, 8, 8);
    expect(extended).toEqual(sel([0, 0], [1, 0]));
    expect(moveSelection(extended, "ArrowRight", true, 8, 8)).toEqual(sel([0, 0], [1, 1]));
    expect(moveSelection(sel([7, 7], [7, 7]), "ArrowDown", true, 8, 8)).toEqual(
      sel([7, 7], [7, 7]),
    );
    expect(moveSelection(start, "a", false, 8, 8)).toBe(start);
  });
});

describe("buildEdits", () => {
  it("sets an engineering value as set_raw and shows the chain", () => {
    const built = buildEdits(fuelTargets, sel([0, 0], [1, 1]), { kind: "set_value", value: 12.5 });
    expect(built.ok).toBe(true);
    if (!built.ok) return;
    expect(built.edits).toEqual([
      {
        table: "fuel_targets",
        slot: 1,
        cells: [
          [0, 0],
          [0, 1],
          [1, 0],
          [1, 1],
        ],
        set_raw: 151,
      },
    ]);
    expect(built.chain).toBe("12.5 AFR → raw 151 → 12.46 AFR");
    expect(built.cells[0]).toMatchObject({ oldRaw: 128, newRaw: 151 });
  });

  it("refuses a value whose raw falls outside u8 before sending, and never clamps", () => {
    // 1881.6 / 5 = 376 > 255; 1881.6 / 0.1 = 18816.
    for (const value of [5, 0.1]) {
      const built = buildEdits(fuelTargets, sel([0, 0], [0, 0]), { kind: "set_value", value });
      expect(built).toMatchObject({ ok: false, reason: "value_out_of_range" });
    }
    const negative = buildEdits(fuelTargets, sel([0, 0], [0, 0]), { kind: "set_value", value: -3 });
    expect(negative).toMatchObject({ ok: false, reason: "value_out_of_range" });
  });

  it("refuses a value no raw decodes to, and a set-to-value on a raw-only table", () => {
    expect(
      buildEdits(fuelTargets, sel([0, 0], [0, 0]), { kind: "set_value", value: 0 }),
    ).toMatchObject({ ok: false, reason: "value_not_invertible" });
    expect(
      buildEdits(kConstant, sel([0, 0], [0, 0]), { kind: "set_value", value: 1.06 }),
    ).toMatchObject({ ok: false, reason: "decode_not_declared" });
  });

  it("scales raw with the declared rounding and sends the factor, not the percent", () => {
    const built = buildEdits(fuelComp, sel([0, 0], [0, 1]), {
      kind: "scale",
      percent: 2,
      rounding: "nearest",
    });
    expect(built.ok).toBe(true);
    if (!built.ok) return;
    expect(built.edits[0]).toMatchObject({ scale: 1.02, rounding: "nearest" });
    // raw 120 and 121 x 1.02 = 122.4 and 123.42
    expect(built.cells.map((cell) => cell.newRaw)).toEqual([122, 123]);
    const ceil = buildEdits(fuelComp, sel([0, 0], [0, 0]), {
      kind: "scale",
      percent: 2,
      rounding: "ceil",
    });
    expect(ceil.ok && ceil.cells[0]?.newRaw).toBe(123);
  });

  it("has no default rounding", () => {
    expect(
      buildEdits(fuelComp, sel([0, 0], [0, 0]), { kind: "scale", percent: 2, rounding: null }),
    ).toMatchObject({ ok: false, reason: "rounding_undeclared" });
  });

  it("does not let float noise move a rounded half: 1.1 x 100 is 110", () => {
    const table: RomTableValues = { ...fuelComp, raw: [[100]], decoded: [[78.125]] };
    const built = buildEdits(table, sel([0, 0], [0, 0]), {
      kind: "scale",
      percent: 10,
      rounding: "nearest",
    });
    expect(built.ok && built.cells[0]?.newRaw).toBe(110);
  });

  it("refuses a scale or add that leaves a cell outside the dtype, naming the cell", () => {
    const high: RomTableValues = { ...fuelComp, raw: [[250]], decoded: [[195]] };
    const scaled = buildEdits(high, sel([0, 0], [0, 0]), {
      kind: "scale",
      percent: 10,
      rounding: "nearest",
    });
    expect(scaled).toMatchObject({ ok: false, reason: "value_out_of_range" });
    expect(scaled.ok === false && scaled.message).toContain("raw 250 becomes 275");
    expect(buildEdits(high, sel([0, 0], [0, 0]), { kind: "add_raw", delta: 6 })).toMatchObject({
      ok: false,
      reason: "value_out_of_range",
    });
    expect(buildEdits(high, sel([0, 0], [0, 0]), { kind: "add_raw", delta: -251 })).toMatchObject({
      ok: false,
      reason: "value_out_of_range",
    });
  });

  it("holds K's be16 range", () => {
    expect(buildEdits(kConstant, sel([0, 0], [0, 0]), { kind: "set_raw", raw: 65535 }).ok).toBe(
      true,
    );
    expect(
      buildEdits(kConstant, sel([0, 0], [0, 0]), { kind: "set_raw", raw: 65536 }),
    ).toMatchObject({ ok: false, reason: "value_out_of_range" });
  });

  it("refuses an edit that changes nothing, a fraction as a raw, and a stray selection", () => {
    expect(
      buildEdits(fuelTargets, sel([0, 0], [0, 0]), { kind: "set_raw", raw: 128 }),
    ).toMatchObject({ ok: false, reason: "no_op_edit" });
    expect(
      buildEdits(fuelTargets, sel([0, 0], [0, 0]), { kind: "add_raw", delta: 1.5 }),
    ).toMatchObject({ ok: false, reason: "not_an_integer" });
    expect(
      buildEdits(fuelTargets, sel([0, 0], [9, 9]), { kind: "set_raw", raw: 130 }),
    ).toMatchObject({ ok: false, reason: "selection_outside_table" });
  });

  it("offers the tables the map marks editable, a slotted one in Map 1 only", () => {
    const map2 = TABLES.tables[2];
    expect(isEditableTable(fuelTargets)).toBe(true);
    expect(isEditableTable(fuelComp)).toBe(true);
    expect(isEditableTable(kConstant)).toBe(true);
    expect(isEditableTable(timingTrimTable("a"))).toBe(true);
    expect(isEditableTable(timingTrimTable("b"))).toBe(true);
    // The trims in Maps 2-5 are not simulated (`rom_map` timing_trim_a_maps_2_5).
    expect(
      isEditableTable({ ...timingTrimTable("a"), id: "timing_trim_a_maps_2_5", slot: 2 }),
    ).toBe(false);
    expect(isEditableTable(map2)).toBe(false);
    expect(buildEdits(map2, sel([0, 0], [0, 0]), { kind: "set_raw", raw: 130 })).toMatchObject({
      ok: false,
      reason: "slot_not_simulated",
    });
    // K is editable in every slot of the map; only Map 1's copy is simulated.
    const k3: RomTableValues = { ...kConstant, slot: 3 };
    expect(isEditableTable(k3)).toBe(false);
    expect(buildEdits(k3, sel([0, 0], [0, 0]), { kind: "set_raw", raw: 130 })).toMatchObject({
      ok: false,
      reason: "slot_not_simulated",
    });
    const unmarked: RomTableValues = {
      ...fuelTargets,
      id: "throttle_area_curve",
      slot: null,
      editable: false,
    };
    expect(isEditableTable(unmarked)).toBe(false);
    expect(buildEdits(unmarked, sel([0, 0], [0, 0]), { kind: "set_raw", raw: 130 })).toMatchObject({
      ok: false,
      reason: "table_not_editable",
    });
    // No table id is listed in the page: any id the map marks is offered.
    const renamed: RomTableValues = { ...fuelTargets, id: "some_other_table" };
    expect(isEditableTable(renamed)).toBe(true);
    expect(isEditableTable({ ...fuelTargets, editable: false })).toBe(false);
  });

  it("counts a pending edit on its own copy only, not on every copy sharing the id", () => {
    const copy = (slot: number): RomTableValues => ({ ...fuelComp, id: "shared_id", slot });
    const pending = (slot: number | null): PendingEdit => ({
      id: `p${slot}`,
      summary: "add 1",
      edit: { table: "shared_id", slot, cells: [[0, 0]], add_raw: 1 },
      tableName: "Fuel Compensation",
      chain: null,
    });
    const list = [pending(2), pending(2), pending(3)];
    expect(pendingEditCount(list, copy(2))).toBe(2);
    expect(pendingEditCount(list, copy(3))).toBe(1);
    expect(pendingEditCount(list, copy(4))).toBe(0);
    expect(pendingEditCount([pending(null)], { ...fuelComp, id: "shared_id", slot: null })).toBe(1);
  });

  it("offers a single-copy table, as on a stock image without map slots", () => {
    const stock: RomTableValues = { ...fuelComp, slot: null };
    expect(isEditableTable(stock)).toBe(true);
    expect(isEditableTable({ ...stock, role: "axis" })).toBe(false);
    const built = buildEdits(stock, sel([0, 0], [0, 0]), { kind: "add_raw", delta: 1 });
    expect(built).toMatchObject({ ok: true });
    expect(built.ok && built.edits).toMatchObject([{ table: "fuel_compensation", slot: null }]);
  });
});

describe("duplicateCells", () => {
  it("finds cells an earlier pending edit already selects", () => {
    const pending: PendingEdit[] = [
      {
        id: "1",
        summary: "",
        tableName: "Fuel Targets",
        chain: null,
        edit: {
          table: "fuel_targets",
          slot: 1,
          cells: [
            [0, 0],
            [0, 1],
          ],
          set_raw: 130,
        },
      },
    ];
    expect(
      duplicateCells(pending, {
        table: "fuel_targets",
        slot: 1,
        cells: [
          [0, 1],
          [0, 2],
        ],
        add_raw: 1,
      }),
    ).toEqual([[0, 1]]);
    expect(
      duplicateCells(pending, { table: "fuel_compensation", slot: 1, cells: [[0, 1]], add_raw: 1 }),
    ).toEqual([]);
  });
});

describe("previewRefusal", () => {
  const refused = new HttpError(
    400,
    JSON.stringify({ error: "edit_refused", reason: "no_op_edit", message: "cell 0,0 unchanged" }),
  );

  it("reads the route's named refusal", () => {
    expect(previewRefusal(refused)).toEqual({
      reason: "no_op_edit",
      message: "cell 0,0 unchanged",
    });
  });

  it("also reads it from FastAPI's detail, and ignores other failures", () => {
    const wrapped = new HttpError(
      400,
      JSON.stringify({ detail: { reason: "duplicate_edit", message: "m" } }),
    );
    expect(previewRefusal(wrapped)?.reason).toBe("duplicate_edit");
    expect(previewRefusal(new HttpError(409, refused.body))).toBeNull();
    expect(previewRefusal(new HttpError(400, "not json"))).toBeNull();
    expect(previewRefusal(new Error("boom"))).toBeNull();
  });
});

describe("persistence and names", () => {
  const pending: PendingEdit[] = [
    {
      id: "1",
      summary: "set raw 130 on 1 cell",
      tableName: "Fuel Targets",
      chain: null,
      edit: { table: "fuel_targets", slot: 1, cells: [[0, 0]], set_raw: 130 },
    },
  ];

  it("round-trips per sha", () => {
    saveEditorState("aaa", { label: "idle", pending });
    expect(loadEditorState("aaa")).toEqual({ label: "idle", pending });
    expect(loadEditorState("bbb")).toEqual({ label: "", pending: [] });
  });

  it("survives corrupt or wrongly shaped storage", () => {
    window.localStorage.setItem(storageKey("aaa"), "{not json");
    expect(loadEditorState("aaa")).toEqual({ label: "", pending: [] });
    window.localStorage.setItem(
      storageKey("aaa"),
      JSON.stringify({ label: 3, pending: [{ nope: true }, ...pending] }),
    );
    expect(loadEditorState("aaa")).toEqual({ label: "", pending });
  });

  it("survives blocked storage", () => {
    const original = Storage.prototype.getItem;
    const setter = Storage.prototype.setItem;
    Storage.prototype.getItem = () => {
      throw new Error("blocked");
    };
    Storage.prototype.setItem = () => {
      throw new Error("blocked");
    };
    try {
      expect(loadEditorState("aaa")).toEqual({ label: "", pending: [] });
      expect(() => saveEditorState("aaa", { label: "x", pending })).not.toThrow();
    } finally {
      Storage.prototype.getItem = original;
      Storage.prototype.setItem = setter;
    }
  });

  it("names the spec file for its label", () => {
    expect(specFileName("idle 2 % richer")).toBe("idle_2_richer.edit-spec.yaml");
    expect(specFileName("  ")).toBe("rom-edit.edit-spec.yaml");
  });

  it("allows simulation only for a matched revision", () => {
    expect(simulateReason({ status: "matched", revision: "ZS44A-20261004" })).toBeNull();
    for (const status of ["reference", "unrecognised", "unknown"]) {
      expect(simulateReason({ status, revision: null })).toBe("base_not_a_recorded_revision");
    }
  });
});

describe("a masked table: the timing trims' flag bit 7 (E09.7)", () => {
  // Trim B: row r holds 43 + r in the low seven bits, and bit 7 is set from column 8 on.
  const trim = timingTrimTable("b");
  const first = trim.raw[0]?.[0];
  const flagged = trim.raw[0]?.[9];

  it("reads the mask from the declared decode, and decodes the value bits only", () => {
    expect(valueMask(trim.decode)).toBe(0x7f);
    expect(valueMask(fuelComp.decode)).toBeNull();
    expect(first).toBe(43);
    expect(flagged).toBe(0x80 | 43);
    expect(valueForRaw(trim.decode, 0x80 | 43)).toBe(43);
    // The value bits of a count have no single raw (the flag is the cell's): the
    // unmasked inverse still refuses, the masked one gives the value bits.
    expect(rawForValue(trim.decode, 50)).toBeNull();
    expect(maskedRawForValue(trim.decode, 50)).toBe(50);
    expect(maskedRawForValue(fuelComp.decode, 50)).toBeNull();
  });

  it("adds counts to the value bits and keeps the flag bit, cell by cell", () => {
    const built = buildEdits(trim, sel([0, 7], [0, 10]), { kind: "add_raw", delta: -3 });
    expect(built.ok).toBe(true);
    if (!built.ok) return;
    expect(built.edits).toEqual([
      {
        table: "timing_trim_b",
        slot: 1,
        cells: [
          [0, 7],
          [0, 8],
          [0, 9],
          [0, 10],
        ],
        add_raw: -3,
      },
    ]);
    // 43 -> 40 in the cells below the flag block, 0xAB -> 0xA8 in it: bit 7 never moves.
    expect(built.cells.map((cell) => [cell.oldRaw, cell.newRaw])).toEqual([
      [43, 40],
      [0x80 | 43, 0x80 | 40],
      [0x80 | 43, 0x80 | 40],
      [0x80 | 43, 0x80 | 40],
    ]);
    expect(built.cells.map((cell) => [cell.oldDecoded, cell.newDecoded])).toEqual([
      [43, 40],
      [43, 40],
      [43, 40],
      [43, 40],
    ]);
  });

  it("scales the value bits and keeps the flag bit", () => {
    const built = buildEdits(trim, sel([0, 9], [0, 9]), {
      kind: "scale",
      percent: 10,
      rounding: "nearest",
    });
    expect(built.ok && built.cells[0]).toMatchObject({ oldRaw: 0x80 | 43, newRaw: 0x80 | 47 });
  });

  it("refuses a result outside the value bits, never carrying it into the flag", () => {
    // 43 + 90 = 133 is a legal raw count (below 255) and an illegal value (above 127).
    expect(buildEdits(trim, sel([0, 0], [0, 0]), { kind: "add_raw", delta: 90 })).toMatchObject({
      ok: false,
      reason: "value_out_of_range",
      message: expect.stringContaining("outside 0..127"),
    });
    // Below zero too: the carry is not borrowed from the flag.
    expect(buildEdits(trim, sel([0, 9], [0, 9]), { kind: "add_raw", delta: -44 })).toMatchObject({
      ok: false,
      reason: "value_out_of_range",
    });
  });

  it("sets a value on the value bits, keeping the flag the selected cells share", () => {
    const built = buildEdits(trim, sel([2, 8], [3, 9]), { kind: "set_value", value: 50 });
    expect(built.ok).toBe(true);
    if (!built.ok) return;
    expect(built.edits[0]).toMatchObject({ table: "timing_trim_b", set_raw: 0x80 | 50 });
    expect(built.chain).toBe("50 count → raw 178 (flag bits 0x80 kept) → 50 count");
    expect(built.cells.every((cell) => cell.newRaw === (0x80 | 50))).toBe(true);
    // A selection with no flag bit sets the plain value.
    const plain = buildEdits(trim, sel([2, 0], [3, 1]), { kind: "set_value", value: 50 });
    expect(plain.ok && plain.edits[0]).toMatchObject({ set_raw: 50 });
  });

  it("refuses one absolute value across cells whose flags differ, by name", () => {
    const mixed = buildEdits(trim, sel([2, 7], [2, 8]), { kind: "set_value", value: 50 });
    // The server's own name for it (`rom_edit_spec` refuses `masked_bits_changed` at the
    // first cell whose flag one set_raw would change); the message says why.
    expect(mixed).toMatchObject({ ok: false, reason: "masked_bits_changed" });
    expect(mixed.ok === false && mixed.message).toContain("different flag bits");
    expect(mixed.ok === false && mixed.message).toContain("use Add or Scale");
    expect(buildEdits(trim, sel([2, 7], [2, 8]), { kind: "set_raw", raw: 50 })).toMatchObject({
      ok: false,
      reason: "masked_bits_changed",
    });
  });

  it("refuses a raw count that would change the flag, as the server does", () => {
    // Raw 50 on a flagged cell would clear bit 7.
    expect(buildEdits(trim, sel([2, 9], [2, 9]), { kind: "set_raw", raw: 50 })).toMatchObject({
      ok: false,
      reason: "masked_bits_changed",
    });
    // Raw 178 keeps it.
    const kept = buildEdits(trim, sel([2, 9], [2, 9]), { kind: "set_raw", raw: 0x80 | 50 });
    expect(kept.ok && kept.cells[0]).toMatchObject({ oldRaw: 0x80 | 45, newRaw: 0x80 | 50 });
    // A raw outside the dtype changes the bits beyond the mask too: the server checks the
    // mask first (`masked_bits_changed`), so the page names it the same way.
    const outside = buildEdits(trim, sel([2, 0], [2, 0]), { kind: "set_raw", raw: 300 });
    expect(outside).toMatchObject({ ok: false, reason: "masked_bits_changed" });
    expect(outside.ok === false && outside.message).toContain("outside a u8 cell's 0..255");
  });

  it("refuses arithmetic on a mask that is not contiguous low bits", () => {
    const gapped: RomTableValues = {
      ...trim,
      decode: {
        ...(trim.decode as RomEditorDecode),
        mask: 0x5a,
        formula: "x = (raw & 0x5A) × 1",
      } as RomTableValues["decode"],
    };
    expect(buildEdits(gapped, sel([0, 0], [0, 0]), { kind: "add_raw", delta: 1 })).toMatchObject({
      ok: false,
      reason: "arithmetic_on_masked_cells",
    });
  });

  it("an unmasked table is exactly as before: the whole raw moves", () => {
    const built = buildEdits(fuelComp, sel([0, 0], [0, 0]), { kind: "add_raw", delta: 200 });
    expect(built).toMatchObject({ ok: false, reason: "value_out_of_range" });
    expect(built.ok === false && built.message).toContain("holds 0..255");
  });
});

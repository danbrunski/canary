import { afterEach, describe, expect, it } from "vitest";

import { HttpError } from "../api/http";
import { TABLES, TABLES_WITH_FUEL_COMP } from "../test/romLibraryFixtures";
import type { CellSelection, PendingEdit, RomEditorDecode } from "../types/romEditor";
import type { RomTableValues } from "../types/romLibrary";
import {
  buildEdits,
  decodeParams,
  duplicateCells,
  isEditableTable,
  loadEditorState,
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

  it("offers only the fuel chain in Map 1", () => {
    const map2 = TABLES.tables[2];
    expect(isEditableTable(fuelTargets)).toBe(true);
    expect(isEditableTable(fuelComp)).toBe(true);
    expect(isEditableTable(kConstant)).toBe(true);
    expect(isEditableTable(map2)).toBe(false);
    expect(buildEdits(map2, sel([0, 0], [0, 0]), { kind: "set_raw", raw: 130 })).toMatchObject({
      ok: false,
      reason: "slot_not_simulated",
    });
    const axis: RomTableValues = { ...fuelTargets, id: "throttle_area_curve", slot: null };
    expect(isEditableTable(axis)).toBe(false);
    expect(buildEdits(axis, sel([0, 0], [0, 0]), { kind: "set_raw", raw: 130 })).toMatchObject({
      ok: false,
      reason: "table_not_editable",
    });
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

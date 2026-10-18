import { afterEach, describe, expect, it } from "vitest";

import { HttpError } from "../api/http";
import { documentedSimFields } from "../test/romSimDoc";
import { fuelCompWithAxes, tablesWithAxes } from "../test/romSimFixtures";
import type { RomTableValues } from "../types/romLibrary";
import {
  EMPTY_FORM,
  KNOWN_ANSWERS_REPRODUCED,
  LOAD_BASIS,
  MAX_DRAWS,
  MAX_GRID_POINTS,
  REMEMBERED_KEYS,
  SIM_REQUEST_FIELDS,
  SIM_STORAGE_KEY,
  centreOffset,
  checkSimForm,
  PEDAL_FLAG_HINT,
  edgesFromBreakpoints,
  editsTouchWotColumn,
  forgetRemembered,
  gridPoints,
  highDetRuledOut,
  knownAnswersLine,
  loadEdgesError,
  loadRemembered,
  newSeed,
  parseEdges,
  parseNumber,
  pedalFlagBit0Clear,
  resultFileName,
  withChoice,
  saveRemembered,
  seedForm,
  simFailure,
  suggestEdges,
  trimLoadEdges,
  wotColumnCaveat,
  wotColumnWarning,
  type SimFormValues,
} from "./romSim";

afterEach(() => window.localStorage.clear());

const EDITS = [
  {
    table: "fuel_compensation",
    slot: 1,
    cells: [[0, 0]] as [number, number][],
    scale: 1.02,
    rounding: "nearest" as const,
  },
];

const FILLED: SimFormValues = {
  grid: "fuel_comp",
  rpmEdges: "200, 600, 1000",
  loadEdges: "3.5, 10.7, 17.8",
  baro: "80",
  iat: "25",
  coolant: "88",
  battery: "14",
  rail: "working_assumption",
  railKpa: "",
  throttle: "20",
  pedalFlag: "0",
  stft: "0",
  ltft: "0",
  throttleClosedLearned: "0",
  idleAirLearned: "0",
  evapPurgeArea: "0",
  highDet: "clear",
  knockControl: "active",
  candidateEvents: "none",
  draws: "200",
  seed: "20261015",
};

describe("the simulate request", () => {
  it("names exactly the 23 fields the doc's request lists", () => {
    const documented = documentedSimFields();
    expect(documented).toHaveLength(23);
    expect([...SIM_REQUEST_FIELDS].sort()).toEqual([...documented].sort());
  });

  it("builds a request with every field from a fully stated form", () => {
    const { request, problems } = checkSimForm(FILLED, " light load ", EDITS);
    expect(problems).toEqual([]);
    expect(request).not.toBeNull();
    expect(Object.keys(request ?? {}).sort()).toEqual([...SIM_REQUEST_FIELDS].sort());
    expect(request).toEqual({
      label: "light load",
      edits: EDITS,
      grid: "fuel_comp",
      rpm_edges: [200, 600, 1000],
      load_edges: [3.5, 10.7, 17.8],
      load_basis: LOAD_BASIS,
      baro_kpa: 80,
      iat_c: 25,
      coolant_c: 88,
      battery_v: 14,
      rail_kpa: "working_assumption",
      throttle_pct: 20,
      pedal_flag: 0,
      stft_pct: 0,
      ltft_pct: 0,
      throttle_closed_learned: 0,
      idle_air_learned: 0,
      evap_purge_area: 0,
      high_det: "clear",
      knock_control: "active",
      candidate_events: "none",
      draws: 200,
      seed: 20261015,
    });
  });

  it("states the HI DET latch and knock control every run: unchosen they block Run, by name", () => {
    const { request, problems } = checkSimForm(
      { ...FILLED, highDet: "", knockControl: "" },
      "light load",
      EDITS,
    );
    expect(request).toBeNull();
    expect(problems.map((item) => [item.field, item.label, item.kind])).toEqual([
      ["highDet", "HI DET latch", "missing"],
      ["knockControl", "Knock control", "missing"],
    ]);
    expect(problems[0]?.message).toContain("nothing logs it");
    for (const [highDet, knockControl, highWord, knockWord] of [
      ["set", "fallback", "set", "fallback"],
      ["clear", "active", "clear", "active"],
      ["set", "active", "set", "active"],
    ] as const) {
      const built = checkSimForm({ ...FILLED, highDet, knockControl }, "x", EDITS);
      expect(built.request).toMatchObject({ high_det: highWord, knock_control: knockWord });
    }
  });

  it("refuses fallback with the latch clear, which is not a running state", () => {
    const built = checkSimForm(
      { ...FILLED, highDet: "clear", knockControl: "fallback" },
      "x",
      EDITS,
    );
    expect(built.request).toBeNull();
    expect(built.problems.map((item) => [item.field, item.kind])).toEqual([["highDet", "invalid"]]);
    expect(built.problems[0]?.message).toContain("the KS fault sets HI DET");
    // The form rules clear out under fallback, and nothing else.
    expect(Object.keys(highDetRuledOut("fallback"))).toEqual(["clear"]);
    expect(highDetRuledOut("active")).toEqual({});
    expect(highDetRuledOut("")).toEqual({});
  });

  it("choosing fallback sets the latch, whatever it was; choosing active leaves it", () => {
    for (const highDet of ["", "set", "clear"] as const) {
      expect(withChoice({ ...FILLED, highDet }, "knockControl", "fallback").highDet).toBe("set");
    }
    expect(withChoice({ ...FILLED, highDet: "clear" }, "knockControl", "active").highDet).toBe(
      "clear",
    );
    expect(withChoice({ ...FILLED, knockControl: "fallback" }, "highDet", "set").highDet).toBe(
      "set",
    );
  });

  it("never remembers the latch or knock control, and never offers a choice preselected", () => {
    // A remembered "set" would answer for a truck whose knock sensor has since been repaired.
    expect(REMEMBERED_KEYS).not.toContain("highDet");
    expect(REMEMBERED_KEYS).not.toContain("knockControl");
    expect(EMPTY_FORM.highDet).toBe("");
    expect(EMPTY_FORM.knockControl).toBe("");
    saveRemembered({ ...FILLED, highDet: "set", knockControl: "fallback" });
    const stored = JSON.parse(window.localStorage.getItem(SIM_STORAGE_KEY) ?? "{}") as {
      values: Record<string, string>;
    };
    expect(Object.keys(stored.values)).not.toContain("highDet");
    expect(Object.keys(stored.values)).not.toContain("knockControl");
    window.localStorage.setItem(
      SIM_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        savedAt: "2026-10-17T00:00:00Z",
        values: { ...stored.values, highDet: "set", knockControl: "fallback" },
      }),
    );
    const seeded = seedForm(EMPTY_FORM, loadRemembered());
    expect(seeded.values.highDet).toBe("");
    expect(seeded.values.knockControl).toBe("");
  });

  it("sends a stated rail pressure as a number", () => {
    const { request } = checkSimForm({ ...FILLED, rail: "stated", railKpa: "380" }, "x", EDITS);
    expect(request?.rail_kpa).toBe(380);
  });

  it("defaults nothing: an empty form has no request and names every missing field", () => {
    const { request, problems } = checkSimForm(EMPTY_FORM, "", []);
    expect(request).toBeNull();
    expect(problems.every((item) => item.kind === "missing")).toBe(true);
    const names = problems.map((item) => item.label);
    expect(names).toEqual(
      expect.arrayContaining([
        "Edits",
        "Label",
        "Grid",
        "RPM edges",
        "Load edges",
        "Baro (kPa)",
        "IAT (°C)",
        "Coolant (°C)",
        "Battery (V)",
        "Throttle (%)",
        "Pedal flag (0-255)",
        "Rail pressure",
        "STFT (%)",
        "LTFT (%)",
        "Throttle closed learned (counts)",
        "Idle air learned (counts)",
        "EVAP purge area (counts)",
        "Candidate events",
        "Draws",
        "Seed",
      ]),
    );
  });

  it("names the one missing field and sends nothing", () => {
    const { request, problems } = checkSimForm({ ...FILLED, baro: "  " }, "x", EDITS);
    expect(request).toBeNull();
    expect(problems).toEqual([
      { field: "baro", label: "Baro (kPa)", kind: "missing", message: "state a value" },
    ]);
  });

  it("names a seed that cannot be sent exactly, and says why", () => {
    const { request, problems } = checkSimForm({ ...FILLED, seed: "9007199254740993" }, "x", EDITS);
    expect(request).toBeNull();
    expect(problems).toHaveLength(1);
    expect(problems[0]).toMatchObject({ field: "seed", label: "Seed", kind: "invalid" });
    expect(problems[0]?.message).toContain("9007199254740993");
    expect(problems[0]?.message).toContain(String(Number.MAX_SAFE_INTEGER));
    expect(problems[0]?.message).toMatch(/exactly/);
    // The largest safe seed is sent as typed.
    const safe = checkSimForm({ ...FILLED, seed: "9007199254740991" }, "x", EDITS);
    expect(safe.request?.seed).toBe(Number.MAX_SAFE_INTEGER);
  });

  it("a zero is a stated value, not a missing one", () => {
    expect(checkSimForm({ ...FILLED, stft: "0", seed: "0" }, "x", EDITS).problems).toEqual([]);
  });

  it("will not send candidate events or a grid nobody chose", () => {
    const { problems } = checkSimForm({ ...FILLED, candidateEvents: "", grid: "" }, "x", EDITS);
    expect(problems.map((item) => item.field)).toEqual(["grid", "candidateEvents"]);
  });

  it("refuses what the server would: bounds, integers, edges", () => {
    const refused = (patch: Partial<SimFormValues>) =>
      checkSimForm({ ...FILLED, ...patch }, "x", EDITS).problems.map((item) => item.field);
    expect(refused({ pedalFlag: "300" })).toEqual(["pedalFlag"]);
    expect(refused({ pedalFlag: "1.5" })).toEqual(["pedalFlag"]);
    expect(refused({ draws: "0" })).toEqual(["draws"]);
    expect(refused({ draws: String(MAX_DRAWS + 1) })).toEqual(["draws"]);
    expect(refused({ seed: "-1" })).toEqual(["seed"]);
    // A seed past 2^53 - 1 would be rounded by Number(): refused, never sent altered.
    expect(refused({ seed: "9007199254740991" })).toEqual([]);
    expect(refused({ seed: "9007199254740992" })).toEqual(["seed"]);
    expect(refused({ seed: "9007199254740993" })).toEqual(["seed"]);
    expect(refused({ seed: "1e20" })).toEqual(["seed"]);
    expect(refused({ baro: "0x50" })).toEqual(["baro"]);
    expect(refused({ baro: "Infinity" })).toEqual(["baro"]);
    expect(refused({ rpmEdges: "200, 200, 300" })).toEqual(["rpmEdges"]);
    expect(refused({ loadEdges: "-1, 50, 120" })).toEqual(["loadEdges"]);
    expect(refused({ loadEdges: "0, 50, 100.5" })).toEqual(["loadEdges"]);
    expect(refused({ loadEdges: "0, 50, 100" })).toEqual([]);
    expect(refused({ rail: "stated", railKpa: "" })).toEqual(["railKpa"]);
  });

  it("refuses a grid over the cell cap before sending", () => {
    const edges = (n: number) => Array.from({ length: n + 1 }, (_, i) => i).join(", ");
    const ok = checkSimForm({ ...FILLED, rpmEdges: edges(32), loadEdges: edges(32) }, "x", EDITS);
    expect(ok.problems).toEqual([]);
    const over = checkSimForm({ ...FILLED, rpmEdges: edges(33), loadEdges: edges(32) }, "x", EDITS);
    expect(over.request).toBeNull();
    expect(over.problems[0]).toMatchObject({ field: "gridPoints", kind: "invalid" });
    expect(over.problems[0]?.message).toContain(`at most ${MAX_GRID_POINTS}`);
  });
});

describe("numbers and edges as typed", () => {
  const spec = { integer: false, min: null, max: null };
  it("accepts plain decimals only", () => {
    expect(parseNumber("80", spec)).toEqual({ kind: "ok", value: 80 });
    expect(parseNumber("-0.5", spec)).toEqual({ kind: "ok", value: -0.5 });
    expect(parseNumber("1e3", spec)).toEqual({ kind: "ok", value: 1000 });
    expect(parseNumber("", spec)).toEqual({ kind: "missing" });
    expect(parseNumber("   ", spec)).toEqual({ kind: "missing" });
    expect(parseNumber("eighty", spec).kind).toBe("invalid");
    expect(parseNumber("12abc", spec).kind).toBe("invalid");
  });

  it("parses edges separated by commas, spaces or newlines", () => {
    expect(parseEdges("1, 2\n3 4;5").edges).toEqual([1, 2, 3, 4, 5]);
    expect(parseEdges("").empty).toBe(true);
    expect(parseEdges("5").error).toMatch(/at least two/);
    expect(parseEdges("1, x").error).toMatch(/"x" is not a number/);
    expect(parseEdges("3, 2").error).toMatch(/rise strictly/);
    expect(parseEdges("1, 2, 3").error).toBeNull();
  });

  it("states the fill model's 0-100 % span", () => {
    expect(loadEdgesError([0, 50, 100])).toBeNull();
    expect(loadEdgesError([-0.1, 50])).toMatch(/below 0/);
    expect(loadEdgesError([50, 100.1])).toMatch(/above 100/);
  });

  it("counts cells as (rpm edges - 1) x (load edges - 1)", () => {
    expect(gridPoints([1, 2, 3, 4], [1, 2, 3])).toBe(6);
    expect(gridPoints([], [1, 2])).toBe(0);
    expect(gridPoints([1], [1, 2])).toBe(0);
  });
});

describe("edges from the base image's Fuel Comp breakpoints", () => {
  it("are the midpoints, extended half a step at each end", () => {
    expect(edgesFromBreakpoints([400, 800, 1200])).toEqual([200, 600, 1000, 1400]);
    expect(edgesFromBreakpoints([10])).toBeNull();
    expect(edgesFromBreakpoints([10, 10, 20])).toBeNull();
    expect(edgesFromBreakpoints([20, 10])).toBeNull();
  });

  it("put each grid point on its breakpoint when the axis is uniform", () => {
    const rpm = Array.from({ length: 16 }, (_, i) => 400 * (i + 1));
    const edges = edgesFromBreakpoints(rpm) ?? [];
    expect(edges).toHaveLength(17);
    expect(edges[0]).toBe(200);
    expect(edges[16]).toBe(6600);
    expect(centreOffset(edges, rpm)).toBeLessThan(1e-9);
    // A non-uniform axis does not, and says by how much of a cell.
    const skewed = [400, 800, 1600];
    expect(centreOffset(edgesFromBreakpoints(skewed) ?? [], skewed)).toBeGreaterThan(0.05);
  });

  it("are read by decode quantity, not by which axis is the rows", () => {
    const normal = suggestEdges(tablesWithAxes(false).tables);
    const swapped = suggestEdges(tablesWithAxes(true).tables);
    expect(normal.rpmEdges).toHaveLength(17);
    expect(normal.rpmEdges?.slice(0, 3)).toEqual([200, 600, 1000]);
    expect(normal.loadEdges?.[0]).toBeCloseTo(3.125, 6);
    expect(normal.notes).toEqual([]);
    expect(swapped.rpmEdges).toEqual(normal.rpmEdges);
    expect(swapped.loadEdges).toEqual(normal.loadEdges);
  });

  it("explain a blank: no decoded load axis, no Fuel Comp table", () => {
    const plain = suggestEdges(
      [fuelCompWithAxes()].map((table) => ({
        ...table,
        axes: { rows: table.axes.rows, cols: { ...(table.axes.cols as object), decoded: null } },
      })) as RomTableValues[],
    );
    expect(plain.rpmEdges).toHaveLength(17);
    expect(plain.loadEdges).toBeNull();
    expect(plain.notes.join(" ")).toMatch(/has no decoded values, so no load edges/);
    const none = suggestEdges([]);
    expect(none.rpmEdges).toBeNull();
    expect(none.notes[0]).toMatch(/no Fuel Compensation \(Map 1\) table/);
  });

  it("trim the load edges to the model's span only when asked, keeping the bounds", () => {
    const edges = suggestEdges(tablesWithAxes().tables).loadEdges ?? [];
    expect(edges.at(-1)).toBeCloseTo(103.125, 6);
    expect(loadEdgesError(edges)).toMatch(/above 100/);
    const trimmed = trimLoadEdges(edges);
    expect(trimmed.at(-1)).toBe(100);
    expect(trimmed.at(-2)).toBeCloseTo(96.875, 6);
    expect(loadEdgesError(trimmed)).toBeNull();
    expect(trimLoadEdges([-3, 5, 20])).toEqual([0, 5, 20]);
    expect(trimLoadEdges([5, 20, 90])).toEqual([5, 20, 90]);
  });
});

describe("what the browser remembers", () => {
  it("round-trips the stated values and nothing about this experiment", () => {
    saveRemembered(FILLED, new Date("2026-10-15T12:30:00Z"));
    const back = loadRemembered();
    expect(back.savedAt).toBe("2026-10-15T12:30:00.000Z");
    expect(back.values).toMatchObject({
      baro: "80",
      draws: "200",
      seed: "20261015",
      rail: "working_assumption",
    });
    // The grid, the edges and the candidate events describe *this* experiment
    // or *this* base image: stated every time.
    expect(Object.keys(back.values)).not.toContain("grid");
    expect(Object.keys(back.values)).not.toContain("rpmEdges");
    expect(Object.keys(back.values)).not.toContain("loadEdges");
    expect(Object.keys(back.values)).not.toContain("candidateEvents");
    expect(REMEMBERED_KEYS).not.toContain("candidateEvents");
  });

  it("seeds only empty fields and reports which came from memory", () => {
    const remembered = {
      values: { baro: "80", iat: "25" } as Partial<SimFormValues>,
      savedAt: null,
    };
    const seeded = seedForm({ ...EMPTY_FORM, iat: "30" }, remembered);
    expect(seeded.values.baro).toBe("80");
    expect(seeded.values.iat).toBe("30");
    expect(seeded.remembered).toEqual(["baro"]);
    expect(seedForm(EMPTY_FORM, { values: {}, savedAt: null }).remembered).toEqual([]);
  });

  it("survives corrupt, wrong-version and blocked storage", () => {
    window.localStorage.setItem(SIM_STORAGE_KEY, "{not json");
    expect(loadRemembered().values).toEqual({});
    window.localStorage.setItem(
      SIM_STORAGE_KEY,
      JSON.stringify({ version: 2, values: { baro: "80" } }),
    );
    expect(loadRemembered().values).toEqual({});
    window.localStorage.setItem(
      SIM_STORAGE_KEY,
      JSON.stringify({ version: 1, values: { baro: 80, iat: "25", rail: "bogus" } }),
    );
    expect(loadRemembered().values).toEqual({ iat: "25" });

    const get = Storage.prototype.getItem;
    const set = Storage.prototype.setItem;
    Storage.prototype.getItem = () => {
      throw new Error("blocked");
    };
    Storage.prototype.setItem = () => {
      throw new Error("blocked");
    };
    try {
      expect(loadRemembered().values).toEqual({});
      expect(() => saveRemembered(FILLED)).not.toThrow();
      expect(() => forgetRemembered()).not.toThrow();
    } finally {
      Storage.prototype.getItem = get;
      Storage.prototype.setItem = set;
    }
  });

  it("forgets", () => {
    saveRemembered(FILLED);
    forgetRemembered();
    expect(loadRemembered().values).toEqual({});
  });

  it("makes a new seed a whole number below 2^31", () => {
    expect(newSeed(() => 0)).toBe(0);
    expect(newSeed(() => 0.5)).toBe(2 ** 30);
    expect(Number.isInteger(newSeed())).toBe(true);
  });
});

describe("the route's failures by name", () => {
  const failure = (status: number, body: unknown) =>
    simFailure(new HttpError(status, typeof body === "string" ? body : JSON.stringify(body)));

  it("reads a refusal with its reason and class", () => {
    expect(
      failure(400, {
        detail: {
          error: "sim_refused",
          kind: "sim_design",
          reason: "grid_too_large",
          message: "40 x 40 = 1600 cells; at most 1024",
        },
      }),
    ).toEqual({
      kind: "refused",
      reason: "grid_too_large",
      message: "40 x 40 = 1600 cells; at most 1024",
      refusalKind: "sim_design",
    });
  });

  it("reads a deferred run, with the profile and the laptop path", () => {
    expect(
      failure(409, {
        detail: {
          error: "analysis_deferred",
          reason: "capture_active",
          profile: "tuning",
          message: "a capture is recording",
          laptop_path: "download the image and run the spec on the laptop",
        },
      }),
    ).toMatchObject({
      kind: "deferred",
      reason: "capture_active",
      profile: "tuning",
      laptopPath: "download the image and run the spec on the laptop",
    });
  });

  it("reads busy, unavailable, pydantic's list, a plain string and anything else", () => {
    expect(
      failure(409, { detail: { error: "analysis_busy", reason: "child_busy", message: "busy" } }),
    ).toMatchObject({ kind: "busy", reason: "child_busy" });
    expect(
      failure(503, {
        detail: { error: "analysis_unavailable", reason: "child_timed_out", message: "900 s" },
      }),
    ).toMatchObject({ kind: "unavailable", reason: "child_timed_out" });
    expect(
      failure(422, {
        detail: [{ loc: ["body", "baro_kpa"], msg: "Field required", type: "missing" }],
      }),
    ).toMatchObject({ kind: "invalid", message: "baro_kpa: Field required" });
    expect(failure(404, { detail: "no ROM library entry 'x'" })).toMatchObject({
      kind: "not_found",
    });
    expect(
      failure(409, { detail: "ROM map definition unavailable for this vehicle: x" }),
    ).toMatchObject({
      kind: "error",
      message: "ROM map definition unavailable for this vehicle: x",
    });
    expect(failure(401, "nope").kind).toBe("unauthorized");
    expect(simFailure(new Error("boom")).kind).toBe("error");
  });
});

describe("files and the E08.7 line", () => {
  it("names the result file safely", () => {
    expect(resultFileName("light load 2 % richer")).toBe("light_load_2_richer.sim-result.json");
    expect(resultFileName("   ")).toBe("rom-sim.sim-result.json");
  });

  it("says what E08.7 has and has not shown, until the flag is flipped", () => {
    // E08.7 was `[~]` in docs/roadmap/tasks/E08-rom-edit-simulation.md on
    // 2026-10-15: items 1-4 pass in the suite through tools/rom_sim.py, the
    // post-flash truck scoring (item 5) is owed. When it is ticked, flip
    // KNOWN_ANSWERS_REPRODUCED and this.
    expect(KNOWN_ANSWERS_REPRODUCED).toBe(false);
    const line = knownAnswersLine();
    expect(line).toMatch(/known answers 1–4/);
    expect(line).toMatch(
      /reproduced in the test suite through the CLI path \(tools\/rom_sim\.py\)/,
    );
    expect(line).toMatch(/not been run through this page on real images yet \(W09\.6/);
    expect(line).toMatch(/post-flash truck drives \(item 5\) is still owed/);
    // It must not claim more than that.
    expect(line).not.toMatch(/not yet reproduced|unvalidated/);
  });
});

describe("the WOT column", () => {
  const tables = tablesWithAxes().tables;
  const targets = (cells: [number, number][]) => ({
    table: "fuel_targets",
    slot: 1,
    cells,
    set_raw: 20,
  });

  it("is named on the pedal flag field: bit 0 forces the last Target AFR column", () => {
    expect(PEDAL_FLAG_HINT).toMatch(/Bit 0 forces the last Target AFR column \(the WOT column\)/);
    expect(PEDAL_FLAG_HINT).toMatch(/bit 0 clear, no simulated point reads that column/);
  });

  it("finds the last column from the loaded table, not from a constant", () => {
    const fuel = tables.find((item) => item.id === "fuel_targets") as RomTableValues;
    expect(fuel.raw[0]).toHaveLength(8);
    expect(editsTouchWotColumn([targets([[2, 7]])], tables)).toBe(true);
    expect(editsTouchWotColumn([targets([[2, 6]])], tables)).toBe(false);
    expect(editsTouchWotColumn([targets([[0, 0]]), targets([[5, 7]])], tables)).toBe(true);
    expect(
      editsTouchWotColumn([{ table: "fuel_targets", slot: 1, all: true, scale: 1 }], tables),
    ).toBe(true);
    // A table of another width has another last column.
    const narrow = [{ ...fuel, raw: fuel.raw.map((row) => row.slice(0, 5)) }];
    expect(editsTouchWotColumn([targets([[2, 7]])], narrow)).toBe(false);
    expect(editsTouchWotColumn([targets([[2, 4]])], narrow)).toBe(true);
  });

  it("ignores the other tables, another slot, and an unknown table", () => {
    expect(
      editsTouchWotColumn(
        [{ table: "fuel_compensation", slot: 1, cells: [[0, 15]], scale: 1.02 }],
        tables,
      ),
    ).toBe(false);
    expect(
      editsTouchWotColumn([{ table: "k_constant", slot: 1, cells: [[0, 0]], set_raw: 5 }], tables),
    ).toBe(false);
    expect(editsTouchWotColumn([{ ...targets([[2, 7]]), slot: 2 }], tables)).toBe(false);
    expect(editsTouchWotColumn([targets([[2, 7]])], [])).toBe(false);
  });

  it("reads the flag as bit 0 clear only when it is a whole number in range", () => {
    expect(pedalFlagBit0Clear("0")).toBe(0);
    expect(pedalFlagBit0Clear(" 2 ")).toBe(2);
    expect(pedalFlagBit0Clear("254")).toBe(254);
    expect(pedalFlagBit0Clear("1")).toBeNull();
    expect(pedalFlagBit0Clear("255")).toBeNull();
    expect(pedalFlagBit0Clear("")).toBeNull();
    expect(pedalFlagBit0Clear("1.5")).toBeNull();
    expect(pedalFlagBit0Clear("300")).toBeNull();
    expect(pedalFlagBit0Clear("x")).toBeNull();
  });

  it("words the warning and the caveat with the stated flag, and offers no value", () => {
    expect(wotColumnWarning(0)).toBe(
      "Your edit touches the WOT column. With pedal flag 0 the simulated points never read it, " +
        "so this run will show no change from this edit. Set pedal flag to 1 (bit 0) to simulate " +
        "wide-open throttle.",
    );
    expect(wotColumnCaveat(4)).toBe(
      "The WOT column was edited with pedal flag 4: this run cannot show that edit's effect.",
    );
  });
});

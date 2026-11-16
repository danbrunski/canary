import { describe, expect, it } from "vitest";

import { SHIPPED_TYPICAL } from "../test/romTypicalFixtures";
import { tablesWithAxes } from "../test/romSimFixtures";
import { suggestEdges, type Remembered } from "./romSim";
import { initSimForm, simFormReducer, type SimFormAction, type SimFormState } from "./romSimForm";
import { presetFill, type SimPreset } from "./romSimSize";

const suggestion = suggestEdges(tablesWithAxes().tables);
const NOTHING: Remembered = { values: {}, savedAt: null };
const REMEMBERED: Remembered = {
  values: { baro: "80", draws: "500" },
  savedAt: "2026-11-01T10:00:00.000Z",
};

const fresh = (remembered = NOTHING) => initSimForm(suggestion, remembered);
const reduce = (state: SimFormState, ...actions: SimFormAction[]) =>
  actions.reduce(simFormReducer, state);
const preset = (name: SimPreset): SimFormAction => {
  const fill = presetFill(name, suggestion);
  if (!fill) throw new Error("fixture has breakpoints");
  return { type: "preset", preset: name, fill };
};
const typicalFilled = (): SimFormAction => ({ type: "typicalFilled", response: SHIPPED_TYPICAL });

describe("initSimForm", () => {
  it("starts empty but for the suggested edges, with nothing marked", () => {
    const state = fresh();
    expect(state.values.baro).toBe("");
    expect(state.values.draws).toBe("");
    expect(state.values.rpmEdges).not.toBe("");
    expect(state.values.loadEdges).not.toBe("");
    expect(state.remembered.size).toBe(0);
    expect(state.savedAt).toBeNull();
    expect(state.typedEdges).toEqual({ rpm: false, load: false });
    expect(state.applied).toBeNull();
    expect(state.typical.size).toBe(0);
    expect(state.typicalState).toEqual({ kind: "idle" });
  });

  it("lays remembered values over empty fields and marks exactly those", () => {
    const state = fresh(REMEMBERED);
    expect(state.values.baro).toBe("80");
    expect(state.values.draws).toBe("500");
    expect([...state.remembered].sort()).toEqual(["baro", "draws"]);
    expect(state.savedAt).toBe(REMEMBERED.savedAt);
  });
});

describe("change", () => {
  it("takes the remembered mark off the edited field only", () => {
    const state = reduce(fresh(REMEMBERED), { type: "change", key: "baro", value: "75" });
    expect(state.values.baro).toBe("75");
    expect([...state.remembered]).toEqual(["draws"]);
  });

  it("keeps the same remembered set when the field was not remembered", () => {
    const before = fresh(REMEMBERED);
    const after = reduce(before, { type: "change", key: "iat", value: "20" });
    expect(after.remembered).toBe(before.remembered);
    expect(after.typical).toBe(before.typical);
  });

  it("takes the typical mark off the edited field and keeps the others", () => {
    const filled = reduce(fresh(), typicalFilled());
    const marked = [...filled.typical.keys()];
    expect(marked.length).toBeGreaterThan(1);
    const [first, ...others] = marked;
    const after = reduce(filled, { type: "change", key: first, value: "1" });
    expect(after.typical.has(first)).toBe(false);
    expect([...after.typical.keys()]).toEqual(others);
  });

  it("says the edges were typed, per axis, once the operator edits one", () => {
    const rpm = reduce(fresh(), { type: "change", key: "rpmEdges", value: "1000 2000" });
    expect(rpm.typedEdges).toEqual({ rpm: true, load: false });
    const both = reduce(rpm, { type: "change", key: "loadEdges", value: "10 20" });
    expect(both.typedEdges).toEqual({ rpm: true, load: true });
  });

  it("sets HI DET when knock control is set to fallback (the one state that pairs)", () => {
    const state = reduce(fresh(), { type: "change", key: "knockControl", value: "fallback" });
    expect(state.values.highDet).toBe("set");
  });
});

describe("presets", () => {
  it("fill the edges and draws, forget a remembered draws, and say what was applied", () => {
    const state = reduce(fresh(REMEMBERED), preset("fine"));
    expect(state.values.draws).toBe("1000");
    expect(state.applied?.preset).toBe("fine");
    expect([...state.remembered]).toEqual(["baro"]);
    expect(state.typedEdges).toEqual({ rpm: false, load: false });
  });

  it("count Coarse's edges as the operator's own, Fine's as the suggestion", () => {
    expect(reduce(fresh(), preset("coarse")).typedEdges).toEqual({ rpm: true, load: true });
    expect(reduce(fresh(), preset("fine")).typedEdges).toEqual({ rpm: false, load: false });
  });

  it("leave the rest of the form alone", () => {
    const before = reduce(fresh(), { type: "change", key: "baro", value: "75" });
    const after = reduce(before, preset("coarse"));
    expect(after.values.baro).toBe("75");
  });
});

describe("resetEdges", () => {
  it("restores the suggested edges and their source note, nothing else", () => {
    const edited = reduce(
      fresh(),
      { type: "change", key: "baro", value: "75" },
      { type: "change", key: "rpmEdges", value: "1 2 3" },
    );
    const after = reduce(edited, {
      type: "resetEdges",
      rpmEdges: suggestion.rpmEdges ?? [],
      loadEdges: suggestion.loadEdges ?? [],
    });
    expect(after.values.rpmEdges).toBe(fresh().values.rpmEdges);
    expect(after.values.baro).toBe("75");
    expect(after.typedEdges).toEqual({ rpm: false, load: false });
  });
});

describe("clearRemembered", () => {
  it("empties exactly the remembered fields and drops the banner's date", () => {
    const edited = reduce(fresh(REMEMBERED), { type: "change", key: "iat", value: "20" });
    const after = reduce(edited, { type: "clearRemembered" });
    expect(after.values.baro).toBe("");
    expect(after.values.draws).toBe("");
    expect(after.values.iat).toBe("20");
    expect(after.remembered.size).toBe(0);
    expect(after.savedAt).toBeNull();
  });
});

describe("typical values", () => {
  it("move through loading to done, with the report of what was filled", () => {
    const loading = reduce(fresh(), { type: "typicalLoading" });
    expect(loading.typicalState).toEqual({ kind: "loading" });
    const done = reduce(loading, typicalFilled());
    expect(done.typicalState.kind).toBe("done");
    expect(done.typical.size).toBeGreaterThan(0);
    for (const key of done.typical.keys()) expect(done.values[key]).not.toBe("");
  });

  it("take the remembered mark off what they overwrote, and no more", () => {
    const state = reduce(fresh(REMEMBERED), typicalFilled());
    expect(state.remembered.has("baro")).toBe(false);
    expect(state.remembered.has("draws")).toBe(true);
    expect(state.values.draws).toBe("500");
  });

  it("report a value the operator stated before they arrived as the one replaced", () => {
    const stated = reduce(fresh(), { type: "change", key: "baro", value: "61" });
    const state = reduce(stated, typicalFilled());
    if (state.typicalState.kind !== "done") throw new Error("expected done");
    const baro = state.typicalState.fill.filled.find((item) => item.field === "baro_kpa");
    expect(baro?.replaced).toBe("61");
  });

  it("leave the form exactly as it was on a failure, and say why", () => {
    const before = fresh(REMEMBERED);
    const failure = { title: "Refused", reason: "typical_values_undeclared", message: "none" };
    const after = reduce(before, { type: "typicalFailed", failure });
    expect(after.values).toBe(before.values);
    expect(after.typicalState).toEqual({ kind: "failed", failure });
  });
});

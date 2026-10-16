import { describe, expect, it } from "vitest";

import { HttpError, NetworkError } from "../api/core";
import { SHIPPED_TYPICAL, leftEmptyOf } from "../test/romTypicalFixtures";
import type { RomTypicalValues } from "../types/romEditor";
import { EMPTY_FORM, SIM_REQUEST_FIELDS, type FormKey, type SimFormValues } from "./romSim";
import {
  NOT_TOUCHED,
  TYPICAL_TARGETS,
  WORKING_ASSUMPTION_TEXT,
  pedalFlagLine,
  placeholderLine,
  typicalFailure,
  typicalFill,
  typicalHeadline,
  typicalTag,
} from "./romSimTypical";

const form = (patch: Partial<SimFormValues> = {}): SimFormValues => ({ ...EMPTY_FORM, ...patch });
const declared = (
  values: RomTypicalValues["values"],
  leftEmpty: RomTypicalValues["left_empty"] = {},
): RomTypicalValues => ({
  profile: "vehicle-x",
  file: "sim_typical_values.yaml",
  file_sha256: "0123456789abcdef".repeat(4),
  values,
  left_empty: leftEmpty,
});
const ALL_LEFT_EMPTY = Object.fromEntries(
  TYPICAL_TARGETS.map((target) => [target.field, { reason: "none stated" }]),
);

/** Every form field the typical-values button is allowed to set. */
const FILLABLE_KEYS = new Set<FormKey>(TYPICAL_TARGETS.flatMap((target) => target.keys));

describe("what the button can fill is the vehicle's state, checked against the data", () => {
  it("is the twelve request fields the shipped file declares, as value or reasoned omission", () => {
    const fields = TYPICAL_TARGETS.map((target) => target.field).sort();
    expect(fields).toHaveLength(12);
    // The Python loader refuses a file that does not partition exactly these twelve.
    const declaredFields = [
      ...Object.keys(SHIPPED_TYPICAL.values),
      ...Object.keys(SHIPPED_TYPICAL.left_empty),
    ].sort();
    expect(fields).toEqual(declaredFields);
    for (const field of fields) expect(SIM_REQUEST_FIELDS).toContain(field);
  });

  it("never includes the experiment's own choices, the edges, the draws, the seed or the label", () => {
    const fields = TYPICAL_TARGETS.map((target) => target.field as string);
    for (const never of [
      "grid",
      "candidate_events",
      "rpm_edges",
      "load_edges",
      "load_basis",
      "draws",
      "seed",
      "label",
      "edits",
    ]) {
      expect(fields).not.toContain(never);
    }
    for (const key of ["grid", "candidateEvents", "rpmEdges", "loadEdges", "draws", "seed"]) {
      expect(FILLABLE_KEYS.has(key as FormKey)).toBe(false);
    }
  });
});

describe("typicalFill", () => {
  it("sets exactly the declared fields, each with its kind and source, and nothing else", () => {
    const fill = typicalFill(form(), SHIPPED_TYPICAL);
    const wantKeys = new Set<string>();
    for (const target of TYPICAL_TARGETS) {
      if (target.field in SHIPPED_TYPICAL.values) for (const key of target.keys) wantKeys.add(key);
    }
    expect(new Set(Object.keys(fill.values))).toEqual(wantKeys);
    expect(Object.keys(fill.values).every((key) => FILLABLE_KEYS.has(key as FormKey))).toBe(true);
    expect(fill.values).toMatchObject({
      baro: "81",
      iat: "25",
      coolant: "88",
      battery: "14",
      throttle: "20",
      pedalFlag: "0",
      stft: "0",
      ltft: "2.34",
      evapPurgeArea: "0",
      throttleClosedLearned: "0",
      idleAirLearned: "0",
      rail: "stated",
      railKpa: "350",
    });
    // The Xterra declares all twelve: nothing is left for the operator in the learned state.
    expect(fill.left).toEqual([]);
    for (const [key, mark] of Object.entries(fill.marks)) {
      expect(fill.values[key as FormKey], key).toBeDefined();
      expect(mark?.source.length).toBeGreaterThan(0);
      expect(["typical", "placeholder"]).toContain(mark?.kind);
    }
    expect(fill.marks.baro).toEqual({
      kind: "typical",
      source: SHIPPED_TYPICAL.values.baro_kpa?.source,
    });
    // The two learned fields no node reads are PLACEHOLDERS, and the mark says so.
    expect(fill.marks.throttleClosedLearned?.kind).toBe("placeholder");
    expect(fill.marks.idleAirLearned?.kind).toBe("placeholder");
    expect(
      Object.entries(fill.marks)
        .filter(([, mark]) => mark?.kind === "placeholder")
        .map(([key]) => key)
        .sort(),
    ).toEqual(["idleAirLearned", "throttleClosedLearned"]);
    expect(fill.filled).toHaveLength(Object.keys(SHIPPED_TYPICAL.values).length);
    expect(fill.skipped).toEqual([]);
    expect(fill.sha8).toBe(SHIPPED_TYPICAL.file_sha256.slice(0, 8));
  });

  it("names the fields a vehicle leaves empty, with the reason it gave, and does not set them", () => {
    const fill = typicalFill(
      form({ throttleClosedLearned: "150", idleAirLearned: "40" }),
      leftEmptyOf("throttle_closed_learned", "idle_air_learned"),
    );
    expect("throttleClosedLearned" in fill.values).toBe(false);
    expect("idleAirLearned" in fill.values).toBe(false);
    expect(fill.left.map((item) => item.field)).toEqual([
      "throttle_closed_learned",
      "idle_air_learned",
    ]);
    for (const item of fill.left) {
      expect(item.declared).toBe(true);
      expect(item.reason).toMatch(/no value is declared for/);
    }
    expect(placeholderLine(fill)).toBeNull();
    expect(typicalHeadline(fill)).toMatch(/^Filled 10 typical values from /);
  });

  it("states what it filled with a headline, the source file and the hash, and counts placeholders apart", () => {
    const fill = typicalFill(form(), SHIPPED_TYPICAL);
    expect(typicalHeadline(fill)).toBe(
      `Filled 12 values (10 typical, 2 placeholders) from nissan-xterra-2008 (sim_typical_values.yaml, sha256 ${SHIPPED_TYPICAL.file_sha256.slice(0, 8)}). Each is marked with its kind and source and is yours to edit.`,
    );
    expect(placeholderLine(fill)).toBe(
      "2 of them are placeholders, not measurements: Throttle closed learned (counts), Idle air learned (counts). Their source says why that is safe and when to replace them.",
    );
    expect(NOT_TOUCHED).toMatch(/grid.*candidate events.*edges.*draws.*seed.*label/);
  });

  it("tags a placeholder `placeholder:`, never `typical:`, from the declared kind alone", () => {
    expect(typicalTag({ kind: "typical", source: "a source" })).toBe("typical: a source");
    expect(typicalTag({ kind: "placeholder", source: "a source" })).toBe("placeholder: a source");
    const one = typicalFill(
      form(),
      declared({ baro_kpa: { value: 81, kind: "placeholder", source: "PLACEHOLDER: x" } }),
    );
    expect(one.marks.baro?.kind).toBe("placeholder");
    expect(placeholderLine(one)).toMatch(/^1 of them is a placeholder, not a measurement: Baro/);
    expect(typicalHeadline(one)).toMatch(/^Filled 1 value \(0 typical, 1 placeholder\) from/);
  });

  it("fills the rail as a choice and a number, or as the plant's working assumption", () => {
    const stated = typicalFill(
      form({ rail: "working_assumption" }),
      declared({ rail_kpa: { value: 350, kind: "typical", source: "s" } }, ALL_LEFT_EMPTY),
    );
    expect(stated.values).toEqual({ rail: "stated", railKpa: "350" });
    const mark = { kind: "typical", source: "s" };
    expect(stated.marks).toEqual({ rail: mark, railKpa: mark });
    const assumed = typicalFill(
      form({ rail: "stated", railKpa: "380" }),
      declared(
        { rail_kpa: { value: "working_assumption", kind: "typical", source: "s" } },
        ALL_LEFT_EMPTY,
      ),
    );
    expect(assumed.values).toEqual({ rail: "working_assumption", railKpa: "" });
    expect(assumed.marks).toEqual({ rail: mark });
    expect(assumed.filled[0]).toMatchObject({
      shown: WORKING_ASSUMPTION_TEXT,
      replaced: "380",
    });
  });

  it("lists what it replaced of the operator's own values, and not a value that is the same number", () => {
    const fill = typicalFill(
      form({
        baro: "80",
        iat: "25.0",
        coolant: "88",
        battery: "13.2",
        rail: "working_assumption",
        throttleClosedLearned: "150",
        idleAirLearned: "0.0",
      }),
      SHIPPED_TYPICAL,
    );
    const replaced = Object.fromEntries(fill.filled.map((item) => [item.field, item.replaced]));
    expect(replaced).toMatchObject({
      baro_kpa: "80",
      iat_c: null, // 25.0 typed, 25 declared: the same number
      coolant_c: null,
      battery_v: "13.2",
      rail_kpa: WORKING_ASSUMPTION_TEXT,
      throttle_pct: null, // it was empty
      throttle_closed_learned: "150", // a placeholder replaces your value too, and says so
      idle_air_learned: null, // 0.0 typed, 0 declared: the same number
    });
  });

  it("overwrites only the fields it fills: a field the vehicle left empty, the draws and the seed keep the operator's value", () => {
    const fill = typicalFill(
      form({ throttleClosedLearned: "150", idleAirLearned: "40", draws: "200", seed: "7" }),
      leftEmptyOf("throttle_closed_learned", "idle_air_learned"),
    );
    expect("throttleClosedLearned" in fill.values).toBe(false);
    expect("idleAirLearned" in fill.values).toBe(false);
    expect("draws" in fill.values).toBe(false);
    expect("seed" in fill.values).toBe(false);
  });

  it("skips a value whose kind is missing or is neither typical nor placeholder", () => {
    const fill = typicalFill(
      form(),
      declared({
        baro_kpa: { value: 81, source: "s" } as unknown as RomTypicalValues["values"][string],
        iat_c: {
          value: 25,
          kind: "measured",
          source: "s",
        } as unknown as RomTypicalValues["values"][string],
        battery_v: { value: 14, kind: "typical", source: "s" },
      }),
    );
    expect(Object.keys(fill.values)).toEqual(["battery"]);
    const why = Object.fromEntries(fill.skipped.map((item) => [item.field, item.why]));
    expect(why.baro_kpa).toMatch(/named no kind/);
    expect(why.iat_c).toMatch(/named no kind/);
  });

  it("skips and names a value with no source, a non-number, one the form refuses, or an unknown field", () => {
    const fill = typicalFill(
      form(),
      declared(
        {
          baro_kpa: { value: 81, kind: "typical", source: "  " },
          iat_c: { value: "25" as unknown as number, kind: "typical", source: "s" },
          pedal_flag: { value: 1.5, kind: "typical", source: "s" },
          coolant_c: { value: Number.NaN, kind: "typical", source: "s" },
          draws: { value: 200, kind: "typical", source: "s" },
          mystery: { value: 1, kind: "typical", source: "s" },
          battery_v: { value: 14, kind: "typical", source: "s" },
        },
        { gadget: { reason: "x" } },
      ),
    );
    expect(Object.keys(fill.values)).toEqual(["battery"]);
    const why = Object.fromEntries(fill.skipped.map((item) => [item.field, item.why]));
    expect(why.baro_kpa).toMatch(/no source/);
    expect(why.iat_c).toMatch(/not a number/);
    expect(why.pedal_flag).toMatch(/not a whole number/);
    expect(why.coolant_c).toMatch(/not a number/);
    expect(why.draws).toMatch(/no form field/);
    expect(why.mystery).toMatch(/no form field/);
    expect(why.gadget).toMatch(/no form field/);
    // A skipped field is not "declared left empty": it is named as skipped, not silently dropped.
    expect(fill.filled.map((item) => item.field)).toEqual(["battery_v"]);
  });

  it("names a fillable field the daemon mentioned in neither list, without a reason it never gave", () => {
    const fill = typicalFill(
      form(),
      declared({ baro_kpa: { value: 81, kind: "typical", source: "s" } }),
    );
    const neither = fill.left.find((item) => item.field === "iat_c");
    expect(neither).toMatchObject({ declared: false });
    expect(neither?.reason).toMatch(/neither a typical value nor a reason/);
    expect(fill.left).toHaveLength(11);
  });

  it("explains the pedal flag it filled in one line, by bit 0", () => {
    expect(typicalFill(form(), SHIPPED_TYPICAL).pedalFlag).toBe(0);
    expect(pedalFlagLine(0)).toMatch(/bit 0 is clear.*WOT column.*shows no change/);
    expect(pedalFlagLine(2)).toMatch(/bit 0 is clear/);
    expect(pedalFlagLine(1)).toMatch(/bit 0 is set.*every simulated point reads/);
    expect(typicalFill(form(), declared({}, ALL_LEFT_EMPTY)).pedalFlag).toBeNull();
  });
});

describe("typicalFailure", () => {
  const http = (status: number, body: unknown) =>
    new HttpError(status, typeof body === "string" ? body : JSON.stringify(body));

  it("names a vehicle that declares none", () => {
    const failure = typicalFailure(
      http(409, {
        detail: {
          error: "typical_values_undeclared",
          message: "vehicle-x has no sim_typical_values.yaml",
        },
      }),
    );
    expect(failure.title).toBe("This vehicle declares no typical values");
    expect(failure.reason).toBe("typical_values_undeclared");
    expect(failure.message).toBe("vehicle-x has no sim_typical_values.yaml");
  });

  it("names a broken file", () => {
    const failure = typicalFailure(
      http(409, {
        detail: {
          error: "typical_values_invalid",
          message: "values.baro_kpa.source: Field required",
        },
      }),
    );
    expect(failure).toMatchObject({
      title: "This vehicle's typical values file is invalid",
      reason: "typical_values_invalid",
    });
  });

  it("names a refused token, an older daemon and an unreachable one, by reason", () => {
    expect(typicalFailure(http(401, { detail: "Unauthorized" })).title).toBe("Not authorized");
    expect(typicalFailure(http(403, "nope")).title).toBe("Not authorized");
    const old = typicalFailure(http(404, { detail: "Not Found" }));
    expect(old.title).toBe("This daemon does not serve typical values");
    expect(old.message).toMatch(/older than this dashboard/);
    const down = typicalFailure(new NetworkError("/control/rom/editor/typical-values"));
    expect(down.message).toMatch(/Can't reach the daemon/);
    expect(typicalFailure(http(500, "boom")).message).toMatch(/Server error 500/);
  });
});

import { describe, expect, it } from "vitest";

import type { RomLeverBlocker, RomLeverEntry, RomLeverOutput, RomLevers } from "../types/romEditor";
import { LEVERS_FUEL_COMP, LEVERS_MAF_CURVE, LEVERS_TIMING_TRIM } from "./romLeverFixtures";

// `satisfies Record<keyof T, true>` makes each list exactly the interface's
// keys (a missing or extra one does not compile), so the fixtures, which
// `tests/test_rom_levers_parity.py` checks against the Python classifier's own
// types, are also checked against the TypeScript ones: the two types cannot
// drift apart without one of the two suites failing.
const LEVERS = {
  schema_version: true,
  tool: true,
  tool_version: true,
  outcome: true,
  reason: true,
  regime: true,
  levers: true,
  outputs: true,
  messages: true,
} satisfies Record<keyof RomLevers, true>;
const ENTRY = {
  lever: true,
  label: true,
  tables: true,
  slots: true,
  byte_class: true,
  start: true,
  end: true,
  changed_bytes: true,
  changed_cells: true,
  nodes: true,
  blockers: true,
} satisfies Record<keyof RomLeverEntry, true>;
const OUTPUT = {
  output: true,
  source: true,
  status: true,
  levers: true,
  blockers: true,
  assumes_conditioned: true,
  refused_if_conditioned: true,
  reached: true,
  number: true,
  design_dependent: true,
} satisfies Record<keyof RomLeverOutput, true>;
const BLOCKER = {
  code: true,
  text: true,
  node: true,
  detail: true,
} satisfies Record<keyof RomLeverBlocker, true>;

const keys = (value: object) => Object.keys(value).sort();

describe("the lever fixtures (real classifier payloads)", () => {
  const cases = {
    fuel_comp: LEVERS_FUEL_COMP,
    timing_trim: LEVERS_TIMING_TRIM,
    maf_curve: LEVERS_MAF_CURVE,
  };

  it.each(Object.entries(cases))("%s has exactly the keys of the TypeScript types", (_, levers) => {
    expect(keys(levers)).toEqual(keys(LEVERS));
    expect(levers.schema_version).toBe(1);
    expect(levers.levers.length).toBeGreaterThan(0);
    for (const entry of levers.levers) {
      expect(keys(entry)).toEqual(keys(ENTRY));
      for (const blocker of entry.blockers) expect(keys(blocker)).toEqual(keys(BLOCKER));
    }
    expect(levers.outputs.length).toBeGreaterThan(0);
    for (const output of levers.outputs) {
      expect(keys(output)).toEqual(keys(OUTPUT));
      for (const blocker of output.blockers) expect(keys(blocker)).toEqual(keys(BLOCKER));
    }
    expect(levers.messages.length).toBeGreaterThan(0);
    expect(levers.messages.every((message) => typeof message === "string")).toBe(true);
  });

  it("covers the three accept cases", () => {
    expect(LEVERS_TIMING_TRIM.messages.join("\n")).toContain(
      "Timing trim A -> ignition_trim_selected: no torque plant",
    );
    expect(LEVERS_MAF_CURVE.messages.join("\n")).toMatch(/0xE27E: not mapped/);
    expect(LEVERS_FUEL_COMP.messages.join("\n")).toContain("closed loop: trim authority undecoded");
  });
});

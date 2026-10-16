// Real `rom_levers.classify_edit(...).to_dict()` payloads, for the ROM Editor's
// tests. They are read from `romLevers.fixture.json`, which
// `tests/test_rom_levers_parity.py` writes (CANARY_UPDATE_LEVERS_FIXTURE=1) from
// the real classifier and checks against its types, so nothing here is
// hand-written: not a message, not a key.
//
//   fuel_comp    the page's preview of a light-load Fuel Comp edit
//   timing_trim  `rom_sim inspect` on a Timing trim A edit (the page cannot make one)
//   maf_curve    `rom_sim inspect` on a changed byte at 0xE27E, in no table (likewise)

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { RomLevers } from "../types/romEditor";

interface LeverCases {
  fuel_comp: RomLevers;
  timing_trim: RomLevers;
  maf_curve: RomLevers;
}

const cases = JSON.parse(
  readFileSync(resolve(process.cwd(), "src/test/romLevers.fixture.json"), "utf8"),
) as LeverCases;

export const LEVERS_FUEL_COMP: RomLevers = cases.fuel_comp;
export const LEVERS_TIMING_TRIM: RomLevers = cases.timing_trim;
export const LEVERS_MAF_CURVE: RomLevers = cases.maf_curve;

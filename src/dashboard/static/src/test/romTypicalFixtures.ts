// The vehicle's real typical values, for the ROM Editor's tests: what
// `GET /control/rom/editor/typical-values` answers on the Xterra profile.
// They are read from `romTypicalValues.fixture.json`, which
// `tests/test_rom_editor.py::test_the_front_end_fixture_is_exactly_what_the_daemon_serves`
// holds equal to `rom_editor.typical_values(PROFILE)` (CANARY_UPDATE_TYPICAL_FIXTURE=1
// rewrites it), so no value or source here is hand-written.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { RomTypicalValues } from "../types/romEditor";

export const SHIPPED_TYPICAL: RomTypicalValues = JSON.parse(
  readFileSync(resolve(process.cwd(), "src/test/romTypicalValues.fixture.json"), "utf8"),
) as RomTypicalValues;

/** The shipped answer with `patch` laid over it (a deep copy: tests mutate nothing shared). */
export function typicalWith(patch: Partial<RomTypicalValues>): RomTypicalValues {
  return { ...structuredClone(SHIPPED_TYPICAL), ...patch };
}

/** The shipped answer with `fields` moved from `values` to `left_empty`: a vehicle
 *  that declares no value for them. Synthetic, for the paths the Xterra's own file
 *  no longer takes (it declares all twelve). */
export function leftEmptyOf(...fields: string[]): RomTypicalValues {
  const answer = structuredClone(SHIPPED_TYPICAL);
  for (const field of fields) {
    delete answer.values[field];
    answer.left_empty[field] = {
      reason: `no value is declared for ${field} on this vehicle (test)`,
    };
  }
  return answer;
}

import { afterEach, beforeEach, vi } from "vitest";

import * as captures from "../api/captures";
import * as jobs from "../api/jobs";
import * as network from "../api/network";
import * as tuning from "../api/tuning";

/** The route modules split out of `api/http` (R28.9). A name a test asks
 *  `mockHttp` for is mocked on whichever of them defines it. */
const SPLIT_MODULES = [
  ["../api/captures", captures],
  ["../api/jobs", jobs],
  ["../api/network", network],
  ["../api/tuning", tuning],
] as const;

/** The `HttpError` stub every panel test substitutes for the real one. */
export class HttpErrorStub extends Error {
  status = 0;
  body = "";
}

/** Ditto for `NetworkError` (an unreachable daemon). `describeError`
 *  narrows on both classes, so both have to exist on the mocked module —
 *  a missing export makes vitest throw on the `instanceof` itself. */
export class NetworkErrorStub extends Error {
  url = "";
}

/** Mocks the route functions named in `names` (`../api/http`'s, or the split
 *  module that now holds them: captures, jobs, network, tuning) and wires the
 *  reset(beforeEach)/clear(afterEach) lifecycle every panel test hand-rolled
 *  (Phase 4.6). Uses `vi.doMock` (not `vi.mock`) deliberately: `vi.mock` is
 *  hoisted by a per-file static scan for the literal call, which corrupts
 *  the closure when the call lives inside an imported helper like this one;
 *  `vi.doMock` is the documented non-hoisted escape hatch. That's safe here
 *  because every caller reaches the mocked module through a *dynamic*
 *  `await import("./Panel")` after calling this, never a static import, so
 *  hoisting was never actually needed. */
export function mockHttp<Name extends string>(
  names: readonly Name[],
): Record<Name, ReturnType<typeof vi.fn>> {
  const mocks = Object.fromEntries(names.map((name) => [name, vi.fn()])) as Record<
    Name,
    ReturnType<typeof vi.fn>
  >;
  const claimed = new Set<string>();
  for (const [path, module] of SPLIT_MODULES) {
    const own = names.filter((name) => name in module);
    for (const name of own) claimed.add(name);
    if (own.length > 0) {
      vi.doMock(path, () => Object.fromEntries(own.map((name) => [name, mocks[name]])));
    }
  }
  vi.doMock("../api/http", () => ({
    ...Object.fromEntries(names.filter((name) => !claimed.has(name)).map((n) => [n, mocks[n]])),
    HttpError: HttpErrorStub,
    NetworkError: NetworkErrorStub,
  }));
  beforeEach(() => {
    for (const name of names) mocks[name].mockReset();
  });
  afterEach(() => vi.clearAllMocks());
  return mocks;
}

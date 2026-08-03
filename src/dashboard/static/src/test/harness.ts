import { afterEach, beforeEach, vi } from "vitest";

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

/** Mocks `../api/http`'s named exports for `names` and wires the
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
  vi.doMock("../api/http", () => ({
    ...mocks,
    HttpError: HttpErrorStub,
    NetworkError: NetworkErrorStub,
  }));
  beforeEach(() => {
    for (const name of names) mocks[name].mockReset();
  });
  afterEach(() => vi.clearAllMocks());
  return mocks;
}

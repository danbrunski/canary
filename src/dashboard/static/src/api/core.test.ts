import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchJson, HttpError, NetworkError } from "./core";

afterEach(() => {
  vi.unstubAllGlobals();
});

/** A transport failure — what the browser throws when the daemon isn't
 *  there at all (Firefox: "NetworkError when attempting to fetch
 *  resource"). Not an `HttpError`: there was no HTTP answer. */
function transportFailure(): TypeError {
  return new TypeError("NetworkError when attempting to fetch resource");
}

describe("fetchJson", () => {
  it("raises NetworkError, tagged with the url, when the request gets no response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(transportFailure()));

    const err = await fetchJson("/control/capture").catch((e: unknown) => e);

    expect(err).toBeInstanceOf(NetworkError);
    expect((err as NetworkError).url).toBe("/control/capture");
    // The browser's own wording is kept as the cause, not the message.
    expect((err as NetworkError).cause).toBeInstanceOf(TypeError);
  });

  it("passes caller-driven aborts through untouched", async () => {
    const abort = new DOMException("The operation was aborted.", "AbortError");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(abort));

    const err = await fetchJson("/status").catch((e: unknown) => e);

    // Callers switch on `name === "AbortError"`; a cancelled poll is not a
    // connectivity problem and must not surface as one.
    expect(err).not.toBeInstanceOf(NetworkError);
    expect((err as Error).name).toBe("AbortError");
  });

  it("still raises HttpError when the daemon answers with a status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        text: () => Promise.resolve('{"detail":"a capture profile is already active"}'),
      }),
    );

    const err = await fetchJson("/control/capture").catch((e: unknown) => e);

    expect(err).toBeInstanceOf(HttpError);
    expect((err as HttpError).status).toBe(409);
  });
});

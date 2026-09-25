import { afterEach, describe, expect, it, vi } from "vitest";

import { controlBlob, fetchJson, HttpError, NetworkError, queryString } from "./core";

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

describe("queryString (R08.4)", () => {
  it("skips null, undefined and empty values and keeps key order", () => {
    expect(queryString({ a: 1, b: null, c: undefined, d: "", e: false, f: "x y" })).toBe(
      "?a=1&e=false&f=x+y",
    );
    expect(queryString({ x_bins: 0 })).toBe("?x_bins=0");
    expect(queryString({ a: null })).toBe("");
  });
});

describe("controlBlob (R08.10)", () => {
  it("sends the bearer token to the base URL and returns the body as a blob", async () => {
    const blob = new Blob(["zip"]);
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      controlBlob("/control/sessions/s/export", "tok", {}, "http://pi:8000/"),
    ).resolves.toBe(blob);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://pi:8000/control/sessions/s/export");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok");
  });

  it("raises HttpError with the body on a refusal", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 404, text: () => Promise.resolve("gone") }),
    );
    await expect(controlBlob("/x", "")).rejects.toMatchObject({ status: 404, body: "gone" });
  });
});

describe("getBinnedAnalysis query (R08.4)", () => {
  it("always sends the required value and x, even empty", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: () => Promise.resolve("{}") });
    vi.stubGlobal("fetch", fetchMock);
    const { getBinnedAnalysis } = await import("./http");
    await getBinnedAnalysis({ value: "", x: "obd:RPM" });
    expect(fetchMock.mock.calls[0][0]).toBe("/analysis/binned?value=&x=obd%3ARPM");
    await getBinnedAnalysis({ value: "obd:MAF", x: "", y: "", xBins: 0, agg: "mean" });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/analysis/binned?value=obd%3AMAF&x=&x_bins=0&agg=mean",
    );
  });
});

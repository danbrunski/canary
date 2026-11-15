import { describe, expect, it, vi } from "vitest";

import { controlJson } from "../api/core";
import {
  allCalls,
  callsTo,
  resetControlRoutesAfterEach,
  routeControl,
  sentTo,
  sequence,
} from "./controlRoutes";

vi.mock("../api/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/core")>()),
  controlJson: vi.fn(),
}));

resetControlRoutesAfterEach();

const post = (body: unknown) => ({ method: "POST", body: JSON.stringify(body) });

describe("routeControl", () => {
  it("answers by METHOD path, a request with no method being a GET", async () => {
    routeControl({ "GET /a": { v: 1 }, "POST /a": { v: 2 } });
    await expect(controlJson("/a", "t")).resolves.toEqual({ v: 1 });
    await expect(controlJson("/a", "t", { method: "POST" })).resolves.toEqual({ v: 2 });
  });

  it("rejects what the table does not list, naming it, so a new request fails its test", async () => {
    routeControl({ "GET /a": {} });
    await expect(controlJson("/b", "t")).rejects.toThrow("unexpected GET /b");
    await expect(controlJson("/a", "t", { method: "DELETE" })).rejects.toThrow(
      "unexpected DELETE /a",
    );
  });

  it("rejects an Error answer and resolves anything else, falsy values included", async () => {
    routeControl({ "GET /down": new Error("pi went away"), "GET /empty": null, "GET /zero": 0 });
    await expect(controlJson("/down", "t")).rejects.toThrow("pi went away");
    await expect(controlJson("/empty", "t")).resolves.toBeNull();
    await expect(controlJson("/zero", "t")).resolves.toBe(0);
  });

  it("calls a function answer with the request; an Error it returns or throws rejects", async () => {
    routeControl({
      "POST /echo": ({ init }) => ({ got: init?.body }),
      "GET /returns": () => new Error("returned"),
      "GET /throws": () => {
        throw new Error("thrown");
      },
    });
    await expect(controlJson("/echo", "t", post({ a: 1 }))).resolves.toEqual({
      got: '{"a":1}',
    });
    await expect(controlJson("/returns", "t")).rejects.toThrow("returned");
    await expect(controlJson("/throws", "t")).rejects.toThrow("thrown");
  });

  it("answers an unlisted request with the fallback, and not before", async () => {
    routeControl({ "GET /a": "listed" }, { fallback: ({ key }) => `fallback ${key}` });
    await expect(controlJson("/a", "t")).resolves.toBe("listed");
    await expect(controlJson("/b", "t")).resolves.toBe("fallback GET /b");
  });

  it("replaces the previous table on each call", async () => {
    routeControl({ "GET /a": 1 });
    routeControl({ "GET /b": 2 });
    await expect(controlJson("/a", "t")).rejects.toThrow("unexpected GET /a");
    await expect(controlJson("/b", "t")).resolves.toBe(2);
  });
});

describe("sequence", () => {
  it("answers each call with the next entry and repeats the last: a read that works once, then fails", async () => {
    routeControl({ "GET /poll": sequence({ n: 1 }, new Error("pi went away")) });
    await expect(controlJson("/poll", "t")).resolves.toEqual({ n: 1 });
    await expect(controlJson("/poll", "t")).rejects.toThrow("pi went away");
    await expect(controlJson("/poll", "t")).rejects.toThrow("pi went away");
  });

  it("calls a function entry with the request", async () => {
    routeControl({ "GET /poll": sequence(({ key }) => key, "second") });
    await expect(controlJson("/poll", "t")).resolves.toBe("GET /poll");
    await expect(controlJson("/poll", "t")).resolves.toBe("second");
  });

  it("needs an entry", () => {
    expect(() => sequence()).toThrow("at least one");
  });
});

describe("recorded calls", () => {
  it("callsTo matches a path exactly, by pattern or by predicate, and by method", async () => {
    routeControl({}, { fallback: {} });
    await controlJson("/x/1", "tok");
    await controlJson("/x/1", "tok", post({ a: 1 }));
    await controlJson("/x/2/simulate", "tok", post({ b: 2 }));
    expect(callsTo("/x/1")).toHaveLength(2);
    expect(callsTo("/x/1", "POST")).toHaveLength(1);
    expect(callsTo("/x/1", "GET")).toHaveLength(1);
    expect(callsTo("/x")).toHaveLength(0);
    expect(callsTo(/\/simulate$/)).toHaveLength(1);
    expect(callsTo((path) => path.startsWith("/x/"))).toHaveLength(3);
    expect(allCalls().map((call) => `${call.method} ${call.path}`)).toEqual([
      "GET /x/1",
      "POST /x/1",
      "POST /x/2/simulate",
    ]);
    expect(callsTo("/x/1", "POST")[0]).toMatchObject({ token: "tok", body: '{"a":1}' });
  });

  it("sentTo is the first matching body parsed, and undefined when nothing was sent", async () => {
    routeControl({}, { fallback: {} });
    expect(sentTo("/x", "POST")).toBeUndefined();
    await controlJson("/x", "t", post({ a: 1 }));
    await controlJson("/x", "t", post({ a: 2 }));
    expect(sentTo("/x", "POST")).toEqual({ a: 1 });
  });
});

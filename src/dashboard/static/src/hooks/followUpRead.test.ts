import { describe, expect, it } from "vitest";

import { HttpError } from "../api/http";
import {
  combineOutcomes,
  followUpFailureText,
  FollowUpReadError,
  reloaded,
  requireFresh,
} from "./followUpRead";

const ok = { status: "ok" } as const;
const abandoned = { status: "abandoned" } as const;
const failed = (error: unknown) => ({ status: "failed", error }) as const;

describe("followUpRead (operator 2026-11-15: a failed follow-up read is loud)", () => {
  it("throws for a failed outcome, saying the action went through and why the read failed", () => {
    expect(() => requireFresh(failed(new Error("boom")))).toThrow(FollowUpReadError);
    expect(() => requireFresh(failed(new Error("boom")))).toThrow(
      "Done, but the refreshed view could not be loaded: boom",
    );
    expect(() => requireFresh(failed(new HttpError(500, "")))).toThrow("Server error 500.");
  });

  it("stays quiet for ok, abandoned and nothing", () => {
    expect(() => requireFresh(ok)).not.toThrow();
    expect(() => requireFresh(abandoned)).not.toThrow();
    expect(() => requireFresh(undefined)).not.toThrow();
  });

  it("combines: failed wins, then abandoned, else ok", () => {
    const bad = failed(new Error("x"));
    expect(combineOutcomes(ok, bad, abandoned)).toBe(bad);
    expect(combineOutcomes(ok, abandoned)).toEqual(abandoned);
    expect(combineOutcomes(ok, ok)).toEqual(ok);
  });

  it("reloaded rejects when any read failed and resolves otherwise", async () => {
    await expect(reloaded(Promise.resolve(ok), Promise.resolve(failed("nope")))).rejects.toThrow(
      "refreshed view could not be loaded: nope",
    );
    await expect(
      reloaded(Promise.resolve(ok), Promise.resolve(abandoned)),
    ).resolves.toBeUndefined();
  });

  it("followUpFailureText is the same message, or null", () => {
    expect(followUpFailureText(ok)).toBeNull();
    expect(followUpFailureText(abandoned)).toBeNull();
    expect(followUpFailureText(failed(new Error("boom")))).toBe(
      "Done, but the refreshed view could not be loaded: boom",
    );
  });
});

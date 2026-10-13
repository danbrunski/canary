import { describe, expect, it } from "vitest";

import type { UpdateSnapshot, UpdateStatus } from "../types/update";
import {
  checkSummary,
  isStarting,
  outcomeNotice,
  parseAwaited,
  updateBlockedReason,
} from "./update";

const AWAITED = { id: "r2", requestedAt: "2026-10-12T10:00:00+00:00" };

function snap(status: Partial<UpdateStatus>, extra: Partial<UpdateSnapshot> = {}): UpdateSnapshot {
  return {
    installed: { version: "0.1.0", sha: "aaa1111" },
    status: { state: "done", ...status },
    last_check: null,
    pending_request: null,
    trigger_installed: true,
    internet: { default_route: true },
    blocked_by: [],
    ...extra,
  };
}

describe("isStarting", () => {
  it("waits while the status still describes an earlier run", () => {
    const earlier = snap({ request_id: "r1", finished_at: "2026-10-12T09:00:00+00:00" });
    expect(isStarting(earlier, AWAITED)).toBe(true);
  });

  it("stops once this request is reported, lost, stuck, or passed by", () => {
    expect(isStarting(snap({ request_id: "r2" }), AWAITED)).toBe(false);
    expect(isStarting(snap({ state: "lost", request_id: "r1" }), AWAITED)).toBe(false);
    const stuck = { action: "update" as const, request_id: "r2", age_s: 90, stuck: true };
    expect(isStarting(snap({ request_id: "r1" }, { pending_request: stuck }), AWAITED)).toBe(false);
    const later = snap({ request_id: null, finished_at: "2026-10-12T10:00:05.123456+00:00" });
    expect(isStarting(later, AWAITED)).toBe(false);
    expect(isStarting(snap({}), null)).toBe(false);
  });
});

describe("update wording", () => {
  it("reports a finished update and ignores a finished check", () => {
    expect(outcomeNotice({ state: "done", action: "update", message: "Updated to b." })).toEqual({
      kind: "ok",
      text: "Updated to b.",
    });
    expect(outcomeNotice({ state: "done", action: "check" })).toBeNull();
    expect(outcomeNotice({ state: "refused", message: "Refused: x." })?.kind).toBe("error");
  });

  it("counts commits and says up to date", () => {
    const base = {
      ok: true,
      reason: null,
      message: null,
      checked_at: "",
      current: "a",
      upstream: "c",
    };
    expect(checkSummary({ ...base, behind: 1 })).toBe("1 commit available: a → c.");
    expect(checkSummary({ ...base, behind: 0 })).toBe("Up to date (a).");
  });

  it("blocks the update with the first reason that applies", () => {
    expect(updateBlockedReason(snap({}, { trigger_installed: false }))).toMatch(
      /canary-update\.path/,
    );
    expect(updateBlockedReason(snap({}, { internet: { default_route: false } }))).toMatch(
      /^No internet/,
    );
    expect(updateBlockedReason(snap({}, { blocked_by: ["capture"] }))).toBe("capture");
    expect(updateBlockedReason(snap({}))).toBeNull();
  });

  it("ignores a stored value that is not an awaited request", () => {
    expect(parseAwaited("r1")).toBeNull();
    expect(parseAwaited(JSON.stringify(AWAITED))).toEqual(AWAITED);
  });
});

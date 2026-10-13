import { afterEach, describe, expect, it } from "vitest";

import { HttpError } from "./api/core";
import type { PairingRequestStatus } from "./api/http";
import {
  ACTIVE_REQUEST_KEY,
  deviceName,
  failureMessage,
  instruction,
  keySteps,
  loadActiveRequest,
  saveActiveRequest,
} from "./pairing";

function status(step: PairingRequestStatus["step"], detail = ""): PairingRequestStatus {
  return {
    id: "r1",
    status: "waiting",
    step,
    detail,
    vehicle_state: "engine_off",
    expires_in_s: 100,
  };
}

describe("deviceName", () => {
  it("names common devices", () => {
    expect(deviceName("Mozilla/5.0 (Linux; Android 14; Pixel 7) Mobile Safari")).toBe(
      "Android phone",
    );
    expect(deviceName("Mozilla/5.0 (Linux; Android 14; SM-X710) Safari")).toBe("Android tablet");
    expect(deviceName("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0)")).toBe("iPhone");
    expect(deviceName("Mozilla/5.0 (Windows NT 10.0; Win64)")).toBe("Windows computer");
    expect(deviceName("curl/8")).toBe("Browser");
  });
});

describe("keySteps", () => {
  it("ticks off the positions the Pi has seen", () => {
    const done = (step: PairingRequestStatus["step"]) =>
      keySteps(step).map((s) => (s.done ? "done" : s.current ? "now" : "-"));
    expect(done("set_acc")).toEqual(["now", "-", "-"]);
    expect(done("turn_on")).toEqual(["done", "now", "-"]);
    expect(done("back_to_acc")).toEqual(["done", "done", "now"]);
    expect(done("unavailable")).toEqual(["-", "-", "-"]);
  });
});

describe("instruction", () => {
  it("never tells the user to switch the key OFF", () => {
    for (const step of ["set_acc", "turn_on", "back_to_acc"] as const) {
      expect(instruction(status(step))).not.toMatch(/turn (it|the key) (to )?off/i);
    }
    expect(instruction(status("set_acc"))).toMatch(/Not OFF/);
  });

  it("passes the Pi's reason through when it cannot see the key", () => {
    expect(instruction(status("unavailable", "A replay is running"))).toBe("A replay is running");
  });
});

describe("failureMessage", () => {
  it("shows the Pi's detail", () => {
    expect(failureMessage(new HttpError(429, JSON.stringify({ detail: "too many" })))).toBe(
      "too many",
    );
  });

  it("explains an unreachable Pi", () => {
    expect(failureMessage(new TypeError("Failed to fetch"))).toMatch(/same WiFi network/);
  });
});

describe("active request", () => {
  afterEach(() => window.sessionStorage.clear());

  it("survives a reload so the phone does not conflict with itself", () => {
    saveActiveRequest({ id: "r1", secret: "s" });
    expect(loadActiveRequest()).toEqual({ id: "r1", secret: "s" });
    saveActiveRequest(null);
    expect(loadActiveRequest()).toBeNull();
  });

  it("ignores junk", () => {
    window.sessionStorage.setItem(ACTIVE_REQUEST_KEY, "{not json");
    expect(loadActiveRequest()).toBeNull();
  });
});

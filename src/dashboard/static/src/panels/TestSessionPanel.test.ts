import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { mockHttp } from "../test/harness";

const mocks = mockHttp([
  "getCurrentCapture",
  "getPreflight",
  "getTestProfiles",
  "startCapture",
  "stopCapture",
  "tagCapture",
]);

const { TestSessionPanel } = await import("./TestSessionPanel");

const PROFILE = {
  id: "wot-pull",
  label: "WOT pull",
  description: "Full-throttle pull",
  protocol: "",
  vehicle_state: "engine_running",
  purpose: "Full-throttle pull",
  order: 1,
  events: [{ tag: "pull_start", label: "Pull start", kind: "event" }],
};

function stubHttp() {
  mocks.getCurrentCapture.mockResolvedValue({ capture: null });
  mocks.getPreflight.mockResolvedValue({ ok: true, checks: [] });
  mocks.getTestProfiles.mockResolvedValue({ profiles: [PROFILE], errors: [] });
}

/** Storage blocked the way a hardened browser blocks it: touching
 *  `window.localStorage` at all throws a SecurityError. */
function blockStorage(): () => void {
  const original = Object.getOwnPropertyDescriptor(window, "localStorage");
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    get() {
      throw new DOMException("The operation is insecure.", "SecurityError");
    },
  });
  return () => {
    if (original) Object.defineProperty(window, "localStorage", original);
  };
}

describe("TestSessionPanel storage", () => {
  let restore: (() => void) | null = null;

  beforeEach(() => {
    window.localStorage.clear();
    stubHttp();
  });

  afterEach(() => {
    restore?.();
    restore = null;
  });

  it("renders the profile picker when localStorage throws (R08.3)", async () => {
    restore = blockStorage();
    render(createElement(TestSessionPanel, { token: "t" }));
    // The profile arrives and the selection effect writes it back to
    // storage: both used to throw straight out of render/commit.
    expect(await screen.findAllByText(/WOT pull/)).not.toHaveLength(0);
  });

  it("restores the stored profile selection", async () => {
    window.localStorage.setItem("canary.testProfile.v1", "wot-pull");
    render(createElement(TestSessionPanel, { token: "t" }));
    expect(await screen.findAllByText(/WOT pull/)).not.toHaveLength(0);
    expect(window.localStorage.getItem("canary.testProfile.v1")).toBe("wot-pull");
  });
});

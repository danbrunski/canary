import { describe, expect, it } from "vitest";

import { HttpError, NetworkError } from "../api/http";
import { describeError } from "./useControlResource";

describe("describeError", () => {
  it("names the unreachable daemon instead of echoing the browser's transport error", () => {
    const text = describeError(
      new NetworkError(
        "http://canary.local:8000/control/capture",
        new TypeError("NetworkError when attempting to fetch resource"),
      ),
    );

    expect(text).toContain("Can't reach the daemon");
    expect(text).toContain("http://canary.local:8000/control/capture");
    // The service worker serves the shell from cache, so "the page loaded"
    // is not evidence the daemon is up — the notice has to say so.
    expect(text).toContain("cached offline");
  });

  it("surfaces the daemon's own reason for a 409 rather than the bare status", () => {
    const err = new HttpError(409, '{"detail":"a capture profile is already active"}');

    expect(describeError(err)).toBe("Refused by the server: a capture profile is already active");
  });

  it("surfaces a 404 detail the same way", () => {
    const err = new HttpError(404, '{"detail":"capture profile not found: fuel-map"}');

    expect(describeError(err)).toBe("Refused by the server: capture profile not found: fuel-map");
  });

  it("falls back to the raw body when the error page isn't FastAPI JSON", () => {
    const err = new HttpError(409, "<html>gateway said no</html>");

    expect(describeError(err)).toBe("Refused by the server: <html>gateway said no</html>");
  });

  it("keeps the existing auth and unwired-feature wording", () => {
    expect(describeError(new HttpError(401, ""))).toBe(
      "Not authorized — enter or check the control token.",
    );
    expect(describeError(new HttpError(503, ""))).toBe(
      "This API isn't available on the daemon (feature not wired).",
    );
    expect(describeError(new HttpError(500, "boom"))).toBe("Server error 500.");
  });
});

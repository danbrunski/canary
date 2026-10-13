import { describe, expect, it } from "vitest";

import type { PairedDevice } from "../api/pairedDevices";
import { formatWhen, isPairingEvent, methodLabel } from "./pairedDevices";

const PHONE: PairedDevice = {
  id: "d1",
  name: "Android phone",
  paired_at: 0,
  method: "key-cycle",
  last_seen: null,
};

describe("methodLabel", () => {
  it("reads each pairing method", () => {
    expect(methodLabel("key-cycle", [])).toBe("key cycle");
    expect(methodLabel("approved-by:shared-token", [])).toBe("approved with the shared token");
    expect(methodLabel("approved-by:loopback", [])).toBe("approved on the Pi itself");
    expect(methodLabel("approved-by:d1", [PHONE])).toBe("approved by Android phone");
    expect(methodLabel("approved-by:gone", [PHONE])).toBe("approved by a removed device (gone)");
    expect(methodLabel("bluetooth-setup", [])).toBe("bluetooth-setup");
  });
});

describe("formatWhen", () => {
  it("says never for a missing stamp", () => {
    expect(formatWhen(null)).toBe("never");
    expect(formatWhen(1_800_000_000)).not.toBe("never");
  });
});

describe("isPairingEvent", () => {
  it("matches pairing.* actions only", () => {
    expect(isPairingEvent({ action: "pairing.requested" })).toBe(true);
    expect(isPairingEvent({ action: "capture.start" })).toBe(false);
    expect(isPairingEvent({ type: "job" })).toBe(false);
  });
});

import { describe, expect, it } from "vitest";
import type { HatStatus, ImuStatus } from "../types";
import { hatSummary, imuHealth } from "./SystemPanel";

const healthy: ImuStatus = {
  fresh: true,
  age_seconds: 0.1,
  configured_hz: 104,
  achieved_hz: 103.9,
  rate_window_seconds: 5,
  samples: 5200,
  overruns: 0,
  undated: 0,
  queue_dropped: 0,
};

describe("imuHealth (H02.5)", () => {
  it("shows the achieved rate against the configured one", () => {
    expect(imuHealth(healthy)).toEqual({ label: "103.9 / 104 Hz", tone: "ok" });
  });

  it("warns on lost samples, overruns or queue drops", () => {
    expect(imuHealth({ ...healthy, overruns: 1 }).tone).toBe("warn");
    expect(imuHealth({ ...healthy, queue_dropped: 3 }).tone).toBe("warn");
  });

  it("names a stale stream by its age, not its last rate", () => {
    expect(imuHealth({ ...healthy, fresh: false, age_seconds: 12.34 })).toEqual({
      label: "stale · 12.3 s",
      tone: "warn",
    });
  });

  it("says no samples before the first one, and no rate without two", () => {
    expect(imuHealth({ ...healthy, age_seconds: null, achieved_hz: null }).label).toBe(
      "no samples",
    );
    expect(imuHealth({ ...healthy, achieved_hz: null }).label).toBe("rate —");
  });
});

const fitted: HatStatus = {
  revision: "2.1",
  source: "eeprom",
  eeprom_product: "CANary-HAT rev 2.1",
  probe: null,
  overlay: "canary-hat-r21",
  overlay_loaded: true,
  power_board: null,
  problems: [],
};

describe("hatSummary", () => {
  it("names the revision and how it was told", () => {
    expect(hatSummary(fitted)).toEqual({ label: "rev 2.1 · eeprom", tone: "ok" });
    expect(hatSummary({ ...fitted, power_board: "1.0" }).label).toBe("rev 2.1 · eeprom · PWR 1.0");
  });

  it("warns on any problem, such as a blank EEPROM", () => {
    const blank = { ...fitted, source: "probed" as const, problems: ["no CANary-HAT ID EEPROM"] };
    expect(hatSummary(blank)).toEqual({ label: "rev 2.1 · probed", tone: "warn" });
  });

  it("says none rather than guessing a revision", () => {
    const none = { ...fitted, revision: null, source: "none" as const, overlay: null };
    expect(hatSummary(none)).toEqual({ label: "none identified", tone: undefined });
    expect(hatSummary({ ...none, problems: ["hardware.kline enabled"] }).tone).toBe("warn");
  });
});

import { describe, expect, it } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { BindingDisplay } from "../types";
import { renderChannel } from "./TelemetryPanel";

const snapshot = (overrides: Partial<ChannelSnapshot["latest"]> = {}): ChannelSnapshot => ({
  latest: {
    timestamp: "2026-05-13T12:00:00+00:00",
    source: "obd",
    channel: "RPM",
    value: 700,
    unit: "rpm",
    ...overrides,
  },
  history: [],
});

const binding = (overrides: Partial<BindingDisplay> = {}): BindingDisplay => ({
  name: "X",
  scale: 1.0,
  offset: 0,
  unit: "unknown",
  confidence: "confirmed",
  plausible_min: null,
  plausible_max: null,
  ...overrides,
});

describe("renderChannel", () => {
  it("passes OBD numeric values through (no binding scale applies)", () => {
    const r = renderChannel(snapshot({ value: 700 }), undefined);
    expect(r.text).toBe("700");
    expect(r.unit).toBe("rpm");
    expect(r.bandClass).toBe("");
  });

  it("applies scale + offset for decoded:* channels", () => {
    const decoded = snapshot({
      source: "decoded",
      channel: "ECM_COOLANT_TEMP",
      value: 105, // raw byte
      unit: "",
    });
    const r = renderChannel(
      decoded,
      binding({ scale: 1.0, offset: -40.0, unit: "degC" }),
    );
    // 105 * 1.0 + (-40) = 65; formatter shows one decimal for 10-100 range
    expect(r.text).toBe("65.0");
    expect(r.unit).toBe("degC");
  });

  it("applies scale 0.125 for RPM-style fractional bindings", () => {
    const decoded = snapshot({ source: "decoded", channel: "ECM_RPM", value: 5600 });
    const r = renderChannel(decoded, binding({ scale: 0.125, offset: 0, unit: "rpm" }));
    // 5600 * 0.125 = 700
    expect(r.text).toBe("700");
    expect(r.unit).toBe("rpm");
  });

  it("does NOT apply scale to OBD channels (python-obd pre-scales those)", () => {
    const obd = snapshot({ source: "obd", channel: "RPM", value: 700 });
    // even if a binding existed for obd:RPM, it shouldn't double-scale
    const r = renderChannel(obd, binding({ scale: 0.125, offset: 0, unit: "rpm" }));
    expect(r.text).toBe("700");
  });

  it("colors band-bad when value is outside plausible range", () => {
    const decoded = snapshot({
      source: "decoded",
      channel: "ECM_COOLANT_TEMP",
      value: 200, // raw → 160°C, way above plausible_max=130
    });
    const r = renderChannel(
      decoded,
      binding({
        scale: 1.0,
        offset: -40,
        unit: "degC",
        plausible_min: -10,
        plausible_max: 130,
      }),
    );
    expect(r.bandClass).toBe("band-bad");
  });

  it("colors band-warn when value is in the top/bottom 5% of plausible range", () => {
    const decoded = snapshot({ source: "decoded", channel: "C", value: 165 });
    // scale=1, offset=-40 → 125; plausible [-10, 130] span 140; top 5% = > 123
    const r = renderChannel(
      decoded,
      binding({
        scale: 1,
        offset: -40,
        unit: "degC",
        plausible_min: -10,
        plausible_max: 130,
      }),
    );
    expect(r.bandClass).toBe("band-warn");
  });

  it("leaves bandClass empty when value is comfortably inside plausible range", () => {
    const decoded = snapshot({ source: "decoded", channel: "C", value: 105 });
    const r = renderChannel(
      decoded,
      binding({
        scale: 1,
        offset: -40,
        unit: "degC",
        plausible_min: -10,
        plausible_max: 130,
      }),
    );
    expect(r.bandClass).toBe("");
  });

  it("renders strings/booleans as-is", () => {
    const vin = snapshot({ value: "JN8AR05Y...", unit: "" });
    const r = renderChannel(vin, undefined);
    expect(r.text).toBe("JN8AR05Y...");
    expect(r.bandClass).toBe("");
  });

  it("renders null / NaN as em-dash", () => {
    expect(renderChannel(snapshot({ value: null }), undefined).text).toBe("—");
    expect(renderChannel(snapshot({ value: NaN }), undefined).text).toBe("—");
  });

  it("falls back to snapshot.unit when binding has no unit", () => {
    const obd = snapshot({ value: 700, unit: "rpm" });
    const r = renderChannel(obd, undefined);
    expect(r.unit).toBe("rpm");
  });

  it("picks appropriate decimal precision by magnitude", () => {
    expect(renderChannel(snapshot({ value: 0.123 }), undefined).text).toBe("0.12");
    expect(renderChannel(snapshot({ value: 12.34 }), undefined).text).toBe("12.3");
    expect(renderChannel(snapshot({ value: 1234.5 }), undefined).text).toBe("1235");
  });
});

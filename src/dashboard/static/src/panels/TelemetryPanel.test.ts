import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { BindingDisplay } from "../types";
import { TelemetryPanel, renderChannel } from "./TelemetryPanel";

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

describe("TelemetryPanel", () => {
  it("hides empty raw CAN frames from counts and the explorer", () => {
    const channels = new Map<string, ChannelSnapshot>();
    for (let i = 0; i < 28; i += 1) {
      const id = `0x${(0x1f9 + i).toString(16).toUpperCase()}`;
      channels.set(
        `can:${id}`,
        snapshot({ source: "can", channel: id, value: null, unit: "" }),
      );
    }
    channels.set(
      "obd:EVAPORATIVE_PURGE",
      snapshot({
        channel: "EVAPORATIVE_PURGE",
        value: 0,
        unit: "%",
      }),
    );
    channels.get("obd:EVAPORATIVE_PURGE")!.history = [0, 1, 0];
    channels.set(
      "obd:FUEL_LEVEL",
      snapshot({
        channel: "FUEL_LEVEL",
        value: 40,
        unit: "%",
      }),
    );
    channels.get("obd:FUEL_LEVEL")!.history = [40, 39.8, 40];

    render(createElement(TelemetryPanel, { channels, bindings: {} }));

    expect(screen.getByText("2 live values")).toBeInTheDocument();
    expect(screen.queryByText("0x1F9")).not.toBeInTheDocument();
    expect(screen.getByText("2 visible")).toBeInTheDocument();
    // No combined multi-channel chart — each tile carries its own sparkline.
    expect(screen.queryByText("Live chart")).not.toBeInTheDocument();
  });
});

describe("TelemetryPanel — Drive (compact) pinning", () => {
  afterEach(() => window.localStorage.clear());

  const defaultDriveKeys = [
    "obd:RPM",
    "obd:SPEED",
    "obd:COOLANT_TEMP",
    "obd:MAF",
    "obd:THROTTLE_POS",
    "obd:CONTROL_MODULE_VOLTAGE",
  ];

  function driveChannels(): Map<string, ChannelSnapshot> {
    const channels = new Map<string, ChannelSnapshot>();
    for (const name of ["ECM_RPM", "ECM_COOLANT_TEMP", "STEERING_ANGLE_002"]) {
      channels.set(
        `decoded:${name}`,
        snapshot({ source: "decoded", channel: name, value: 1234, unit: "" }),
      );
      channels.get(`decoded:${name}`)!.history = [1, 2, 3];
    }
    return channels;
  }

  function defaultDriveChannels(): Map<string, ChannelSnapshot> {
    const channels = new Map<string, ChannelSnapshot>();
    for (const key of defaultDriveKeys) {
      const channel = key.slice(key.indexOf(":") + 1);
      channels.set(key, snapshot({ source: "obd", channel, value: 42, unit: "" }));
      channels.get(key)!.history = [40, 41, 42];
    }
    return channels;
  }

  it("shows every channel as an equal tile — nothing hidden behind an expander", () => {
    render(createElement(TelemetryPanel, { channels: driveChannels(), bindings: {}, compact: true }));
    // No "N more channels" expander, no pinned-only toggle until something is pinned.
    expect(screen.queryByText(/more channels/)).not.toBeInTheDocument();
    expect(screen.queryByText(/pinned only/)).not.toBeInTheDocument();
    // Every channel is an equal tile — assert the tile-unique pin control.
    expect(screen.getByRole("button", { name: "pin ECM_RPM" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "pin ECM_COOLANT_TEMP" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "pin STEERING_ANGLE_002" }),
    ).toBeInTheDocument();
  });

  it("pins a channel, persists it, and exposes the pinned-only toggle", () => {
    render(createElement(TelemetryPanel, { channels: driveChannels(), bindings: {}, compact: true }));
    fireEvent.click(screen.getByRole("button", { name: "pin ECM_RPM" }));
    expect(screen.getByText("3 live values · 1 pinned")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /pinned only/ }),
    ).toBeInTheDocument();
    expect(
      JSON.parse(window.localStorage.getItem("canary.drive.pinned") ?? "[]"),
    ).toEqual(["decoded:ECM_RPM"]);
    // Unpin returns to the no-pins state.
    fireEvent.click(screen.getByRole("button", { name: "unpin ECM_RPM" }));
    expect(screen.getByText("3 live values")).toBeInTheDocument();
  });

  it("does not re-seed default pins after every default pin is removed", async () => {
    render(createElement(TelemetryPanel, { channels: defaultDriveChannels(), bindings: {}, compact: true }));

    expect(await screen.findByText("6 live values · 6 pinned")).toBeInTheDocument();
    for (const key of defaultDriveKeys) {
      const name = key.slice(key.indexOf(":") + 1);
      fireEvent.click(screen.getByRole("button", { name: `unpin ${name}` }));
    }

    expect(screen.getByText("6 live values")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /pinned only/ })).not.toBeInTheDocument();
    expect(JSON.parse(window.localStorage.getItem("canary.drive.pinned") ?? "null")).toEqual([]);
    for (const key of defaultDriveKeys) {
      const name = key.slice(key.indexOf(":") + 1);
      expect(screen.getByRole("button", { name: `pin ${name}` })).toBeInTheDocument();
    }
  });

  it("honors a stored empty pin list", () => {
    window.localStorage.setItem("canary.drive.pinned", "[]");

    render(createElement(TelemetryPanel, { channels: defaultDriveChannels(), bindings: {}, compact: true }));

    expect(screen.getByText("6 live values")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /pinned only/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "pin RPM" })).toBeInTheDocument();
  });

  it("sparklines are off by default and the per-card toggle persists", () => {
    render(createElement(TelemetryPanel, { channels: driveChannels(), bindings: {}, compact: true }));
    // Default: graph off → "show graph" affordance on every tile, no canvas.
    const showBtn = screen.getByRole("button", { name: "show graph for ECM_RPM" });
    expect(showBtn).toHaveAttribute("aria-pressed", "false");
    expect(document.querySelector("canvas")).toBeNull();

    fireEvent.click(showBtn);
    expect(
      screen.getByRole("button", { name: "hide graph for ECM_RPM" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(document.querySelector("canvas")).not.toBeNull();
    expect(
      JSON.parse(window.localStorage.getItem("canary.drive.graphs") ?? "[]"),
    ).toEqual(["decoded:ECM_RPM"]);

    // Toggling back removes it again.
    fireEvent.click(screen.getByRole("button", { name: "hide graph for ECM_RPM" }));
    expect(
      JSON.parse(window.localStorage.getItem("canary.drive.graphs") ?? "[]"),
    ).toEqual([]);
  });
});

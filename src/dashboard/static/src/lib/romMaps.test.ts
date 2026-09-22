import { describe, expect, it } from "vitest";
import { axisBracket, liveValue, mapColor, tracePosition } from "./romMaps";
import type { RomTable } from "./romMaps";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";

const table: RomTable = {
  slot: 1, kind: "fuel_targets", title: "Target AFR", status: "available", reason: null,
  x_label: "BFS", x_unit: "ms", y_label: "RPM", y_unit: "rpm", unit: "AFR",
  x_axis: [8, 12], y_axis: [1200, 2000], values: [[14.7, 13], [13, 12]], raw: [[128, 145], [145, 157]],
};

describe("ROM map trace", () => {
  it("marks an exact cell or surrounding breakpoints without claiming interpolation", () => {
    expect(axisBracket([1, 2, 3], 2)).toEqual([1]);
    expect(axisBracket([1, 2, 3], 1.5)).toEqual([0, 1]);
    expect(tracePosition(table, 1900, 11)).toEqual({ rows: [0, 1], columns: [0, 1], row: 1, column: 1 });
    expect(tracePosition(table, 1200, 8)).toEqual({ rows: [0], columns: [0], row: 0, column: 0 });
    expect(tracePosition(table, 2100, 8)).toBeNull();
    expect(tracePosition(table, 1200, 7)).toBeNull();
    expect(tracePosition({ ...table, kind: "fuel_compensation" }, 1200, 8)).toBeNull();
  });

  it("uses provider scaling once and refuses stale/non-numeric/future samples", () => {
    const now = Date.parse("2026-09-21T12:00:00Z");
    const channels = new Map<string, ChannelSnapshot>([["did:BFS", {
      latest: { timestamp: new Date(now).toISOString(), source: "did", channel: "BFS", value: 20000, unit: "counts" }, history: [],
    }]]);
    const provider = { spec: "did:BFS", transform: "scale" as const, factor: 0.00049 };
    expect(liveValue(channels, provider, now, 14.7)?.value).toBeCloseTo(9.8);
    expect(liveValue(channels, provider, now + 2501, 14.7)).toBeNull();
    expect(liveValue(channels, provider, now - 2000, 14.7)).toBeNull();
    channels.get("did:BFS")!.latest.value = "20000";
    expect(liveValue(channels, provider, now, 14.7)).toBeNull();
  });

  it("does not carry a commanded target across a fuel-cut sentinel", () => {
    const now = Date.parse("2026-09-21T12:00:00Z");
    const row = { timestamp: new Date(now).toISOString(), source: "mem", channel: "PHI", value: 1.2, unit: "phi" };
    const channels = new Map<string, ChannelSnapshot>([["mem:PHI", { latest: row, history: [] }]]);
    const provider = { spec: "mem:PHI", transform: "equiv_ratio_afr" as const, factor: null };
    expect(liveValue(channels, provider, now, 14.7)?.value).toBeCloseTo(12.25);
    channels.set("mem:PHI_SENTINEL", { latest: { ...row, value: 0 }, history: [] });
    expect(liveValue(channels, provider, now, 14.7)).toBeNull();
  });

  it("finds a mem: provider on its obd:mem: stream key, where memory rows actually arrive", () => {
    const now = Date.parse("2026-09-21T12:00:00Z");
    const row = { timestamp: new Date(now).toISOString(), source: "obd", channel: "mem:PHI", value: 1.2, unit: "phi" };
    const channels = new Map<string, ChannelSnapshot>([["obd:mem:PHI", { latest: row, history: [] }]]);
    const provider = { spec: "mem:PHI", transform: "equiv_ratio_afr" as const, factor: null };
    expect(liveValue(channels, provider, now, 14.7)?.value).toBeCloseTo(12.25);
    channels.set("obd:mem:PHI_SENTINEL", { latest: { ...row, channel: "mem:PHI_SENTINEL", value: 0 }, history: [] });
    expect(liveValue(channels, provider, now, 14.7)).toBeNull();
  });

  it("uses green-yellow-orange-red with finite constant-table colors", () => {
    expect(mapColor(0, 0, 3)).toBe("rgb(80, 190, 110)");
    expect(mapColor(1, 0, 3)).toBe("rgb(245, 210, 65)");
    expect(mapColor(2, 0, 3)).toBe("rgb(248, 155, 55)");
    expect(mapColor(3, 0, 3)).toBe("rgb(230, 80, 65)");
    expect(mapColor(1, 1, 1)).not.toContain("NaN");
  });
});

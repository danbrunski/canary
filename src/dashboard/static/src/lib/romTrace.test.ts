import { describe, expect, it } from "vitest";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { CaptureProfile, StatusResponse } from "../types";
import type { RomTable } from "./romMaps";
import {
  addCoverage,
  axisPosition,
  cellDisagreement,
  coverageClass,
  coverageColor,
  describeClamp,
  describeDisagreement,
  ecmDisplay,
  ecmPoint,
  ecmPosition,
  ecmValueRefusal,
  emptyCoverage,
  mappingRefusal,
  profilesCarrying,
  residencyMatchesTable,
  traceBreakpoints,
  traceLiveness,
  traceGate,
  traceSample,
} from "./romTrace";
import type { CellResidency, TableTrace } from "./romTrace";

const now = Date.parse("2026-09-21T12:00:00Z");
const X = "mem:ECM_FUEL_TARGET_MAP_X_TRACE";
const Y = "mem:ECM_ENGINE_SPEED_RAM";
const table: RomTable = {
  slot: 1,
  kind: "fuel_targets",
  title: "Target AFR",
  status: "available",
  reason: null,
  x_label: "BFS",
  x_unit: "ms",
  y_label: "RPM",
  y_unit: "rpm",
  unit: "AFR",
  x_axis: [8, 12, 16],
  y_axis: [1200, 2000],
  values: [
    [14.7, 13, 12],
    [13, 12, 11],
  ],
  raw: [
    [1, 1, 1],
    [1, 1, 1],
  ],
};
const verified: TableTrace = {
  x: {
    channel: X,
    axis_mapping: "verified",
    axis_units: "display",
    scale: 0.5,
    offset: 1,
    evidence: "synthetic",
    request_channel: "mem:fuel_axis",
    stored_decode: { scale: 0.250859, offset: 0 },
  },
  y: {
    channel: Y,
    axis_mapping: "verified",
    axis_units: "display",
    scale: 1,
    offset: 0,
    evidence: "rpm",
    request_channel: "mem:spark",
    stored_decode: { scale: 12.5, offset: 0 },
  },
  unavailable_note: "retired",
  out_of_range: null,
};
const unverified: TableTrace = {
  ...verified,
  x: {
    ...verified.x,
    axis_mapping: "unverified",
    axis_units: null,
    scale: null,
    offset: null,
    evidence: "Not settled.",
  },
};
/** W05.1 as shipped: the X byte (stored raw × 0.250859) lands on the ROM's
 *  raw BFS axis bytes unchanged, rpm on the rpm axis, and both clamp. */
const X_STORED_SCALE = 0.250859;
const shipped: TableTrace = {
  x: {
    ...verified.x,
    axis_units: "raw",
    scale: 1,
    offset: 0,
    evidence: "firmware",
    stored_decode: { scale: X_STORED_SCALE, offset: 0 },
  },
  y: { ...verified.y, scale: 1, offset: 0 },
  unavailable_note: null,
  out_of_range: { behaviour: "clamp", evidence: "0x026F40" },
};
const shippedTable: RomTable = {
  ...table,
  x_axis: [8.76, 11.46, 15.67, 16.85, 18.2, 18.54, 21.4, 21.57],
  x_axis_raw: [0x34, 0x44, 0x5d, 0x64, 0x6c, 0x6e, 0x7f, 0x80],
  y_axis: [1200, 1600, 2000, 2800, 3600, 4000, 4400, 5200],
  values: Array.from({ length: 8 }, () => Array.from({ length: 8 }, () => 14.7)),
  raw: Array.from({ length: 8 }, () => Array.from({ length: 8 }, () => 128)),
  trace: shipped,
};
const row = (channel: string, value: unknown, at = now): [string, ChannelSnapshot] => [
  `obd:${channel}`,
  {
    latest: {
      timestamp: new Date(at).toISOString(),
      source: "obd",
      channel,
      value: value as number,
      unit: null,
    },
    history: [],
  },
];
const status = (state: string, extra: Record<string, unknown> = {}) =>
  ({ vehicle_state: { state }, ...extra }) as unknown as StatusResponse;

describe("ECM trace mapping", () => {
  it("reads memory channels by their obd:mem: stream key and refuses stale samples", () => {
    const channels = new Map([row(X, 30)]);
    expect(traceSample(channels, X, now)?.value).toBe(30);
    expect(traceSample(channels, X, now + 3001)).toBeNull();
    expect(traceSample(new Map([row(X, "sentinel")]), X, now)).toBeNull();
  });

  it("names the unverified axis and places no ECM point on it", () => {
    expect(mappingRefusal(unverified)).toMatch(
      /X trace \(mem:ECM_FUEL_TARGET_MAP_X_TRACE\).*not verified\. Not settled\./,
    );
    expect(mappingRefusal(null)).toMatch(/maps no trace channels/);
    expect(mappingRefusal(verified)).toBeNull();
    const channels = new Map([row(X, 22), row(Y, 1990)]);
    expect(ecmPoint(unverified, channels, now)).toBeNull();
  });

  it("maps a verified trace into axis units and picks the nearest cell", () => {
    const channels = new Map([row(X, 22), row(Y, 1990, now - 100)]);
    const point = ecmPoint(verified, channels, now);
    expect(point).toMatchObject({ x: 12, y: 1990 });
    expect(ecmPosition({ ...table, trace: verified }, point)).toMatchObject({ row: 1, column: 1 });
    expect(ecmPoint(verified, new Map([row(X, 22), row(Y, 1990, now - 900)]), now)).toBeNull();
  });
});

describe("ECM trace on the raw BFS axis (W05.1)", () => {
  const at = (raw: number, rpm: number) => new Map([row(X, raw * X_STORED_SCALE), row(Y, rpm)]);

  it("lands an X byte equal to an axis breakpoint exactly on that column", () => {
    expect(traceBreakpoints(shippedTable, "x")).toEqual(shippedTable.x_axis_raw);
    shippedTable.x_axis_raw!.forEach((raw, column) => {
      const point = ecmPoint(shipped, at(raw, 2000), now);
      expect(point?.x).toBe(raw);
      expect(axisPosition(shippedTable.x_axis_raw!, point!.x, true)).toEqual({
        position: column,
        edge: null,
      });
      expect(ecmPosition(shippedTable, point)).toMatchObject({ row: 2, column, columns: [column] });
      expect(ecmDisplay(shippedTable, point!)?.x).toBeCloseTo(shippedTable.x_axis[column], 12);
    });
  });

  it("interpolates an X byte between breakpoints proportionally", () => {
    // 80 counts is 12/25 of the way from 0x44 (68) to 0x5D (93).
    const point = ecmPoint(shipped, at(80, 1400), now);
    expect(point).toMatchObject({ x: 80, y: 1400 });
    expect(axisPosition(shippedTable.x_axis_raw!, 80, true)?.position).toBeCloseTo(1.48, 12);
    expect(ecmPosition(shippedTable, point)).toMatchObject({
      row: 0, // 1,400 rpm is the exact midpoint: ties go low
      column: 1,
      rows: [0, 1],
      columns: [1, 2],
      edges: { x: null, y: null },
    });
    expect(ecmDisplay(shippedTable, point!)?.x).toBeCloseTo(11.46 + (15.67 - 11.46) * 0.48, 12);
  });

  it("reads the edge cell past the axes only where the profile declares the clamp", () => {
    const idle = ecmPoint(shipped, at(17, 750), now);
    const position = ecmPosition(shippedTable, idle);
    expect(position).toMatchObject({ row: 0, column: 0, edges: { x: "below", y: "below" } });
    expect(describeClamp(position!, shippedTable)).toMatch(
      /BFS coordinate is below the first breakpoint and the ECM's RPM coordinate is below the first breakpoint, and the ECM's lookup reads the edge/,
    );
    expect(ecmDisplay(shippedTable, idle!)).toMatchObject({ x: 8.76, edges: { x: "below" } });
    const noClamp = { ...shippedTable, trace: { ...shipped, out_of_range: null } };
    expect(ecmPosition(noClamp, idle)).toBeNull();
    const d = cellDisagreement(
      { rows: [0], columns: [0], row: 0, column: 0 },
      { rows: [0, 1], columns: [0, 1], row: 0, column: 1 },
      ecmDisplay(shippedTable, idle!)!,
      { x: 11, y: 1300 },
    );
    expect(describeDisagreement(d!, shippedTable)).toContain("(ECM ≤8.76 vs inferred 11.00 ms)");
  });

  it("never compares a stored (scaled) value with raw bytes", () => {
    // Without the ROM's raw axis bytes there is nothing to land on: refused
    // by name rather than falling back to the displayed axis.
    const noRaw = { ...shippedTable, x_axis_raw: undefined };
    expect(mappingRefusal(shipped, noRaw)).toMatch(/raw axis bytes, which this ROM inspection/);
    expect(ecmPosition(noRaw, ecmPoint(shipped, at(68, 2000), now))).toBeNull();
    const noDecode = { ...shipped, x: { ...shipped.x, stored_decode: null } };
    expect(mappingRefusal(noDecode)).toMatch(/names no decode to undo/);
    expect(ecmPoint(noDecode, at(68, 2000), now)).toBeNull();
    // A value stored under another decode is not a whole count: no point.
    const other = new Map([row(X, 68 * 0.25), row(Y, 2000)]);
    expect(ecmPoint(shipped, other, now)).toBeNull();
    expect(ecmValueRefusal(shipped, other, now)).toMatch(/does not return to a whole raw count/);
    expect(ecmValueRefusal(shipped, at(68, 2000), now)).toBeNull();
  });

  it("measures disagreement in rows and columns, and none when the cells agree", () => {
    const ecm = { rows: [1], columns: [2], row: 1, column: 2 };
    const inferred = { rows: [0, 1], columns: [0, 1], row: 0, column: 1 };
    expect(cellDisagreement(ecm, inferred, { x: 16, y: 2000 }, { x: 13, y: 1500 })).toMatchObject({
      rows: 1,
      columns: 1,
    });
    expect(cellDisagreement(ecm, ecm, { x: 16, y: 2000 }, { x: 16, y: 2000 })).toBeNull();
    expect(cellDisagreement(null, inferred, { x: 0, y: 0 }, { x: 0, y: 0 })).toBeNull();
  });
});

describe("ECM trace liveness (W05.4)", () => {
  const polled = {
    capture_profile: {
      name: "tuning",
      memory: ["ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_ENGINE_SPEED_RAM"],
      mode22_enabled: true,
    },
  };

  it("is live only when every trace channel is fresh", () => {
    expect(
      traceLiveness(verified, new Map([row(X, 1), row(Y, 1)]), now, status("engine_running"), null)
        .state,
    ).toBe("live");
    expect(
      traceLiveness(verified, new Map([row(X, 1)]), now, status("engine_running", polled), null)
        .state,
    ).toBe("not_answering");
  });

  it("separates no ECM data, not polled and not answering", () => {
    const empty = new Map<string, ChannelSnapshot>();
    expect(traceLiveness(verified, empty, now, status("engine_off"), null).state).toBe(
      "no_ecm_data",
    );
    expect(traceLiveness(verified, empty, now, null, null).state).toBe("no_ecm_data");
    const notPolled = traceLiveness(
      verified,
      empty,
      now,
      status("engine_running", {
        capture_profile: { name: "tuning", memory: [], mode22_enabled: true },
      }),
      [],
    );
    expect(notPolled).toMatchObject({ state: "not_polled", profiles: [] });
    if (notPolled.state === "not_polled") expect(notPolled.message).toContain("retired");
    const disabled = traceLiveness(
      verified,
      empty,
      now,
      status("ignition_only", {
        capture_profile: { ...polled.capture_profile, mode22_enabled: false },
      }),
      null,
    );
    expect(disabled.state).toBe("not_polled");
    const silent = traceLiveness(verified, empty, now, status("engine_running", polled), null);
    expect(silent.state).toBe("not_answering");
    if (silent.state === "not_answering")
      expect(silent.message).toContain("no scheduler request mem:fuel_axis found");
  });

  it("names only profiles that carry every trace channel", () => {
    const profiles = [
      { name: "tuning", memory: ["ECM_ENGINE_SPEED_RAM"], mode22_enabled: true },
      {
        name: "rom-trace",
        memory: ["ecm_fuel_target_map_x_trace", "ECM_ENGINE_SPEED_RAM"],
        mode22_enabled: true,
      },
      {
        name: "off",
        memory: ["ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_ENGINE_SPEED_RAM"],
        mode22_enabled: false,
      },
    ] as unknown as CaptureProfile[];
    expect(profilesCarrying(verified, profiles)).toEqual(["rom-trace"]);
  });
});

describe("coverage (W05.7/W05.8)", () => {
  it("accumulates per cell and classifies never-visited apart from thin", () => {
    let grid = emptyCoverage(table);
    for (let i = 0; i < 6; i += 1)
      grid = addCoverage(grid, { rows: [1], columns: [2], row: 1, column: 2 });
    grid = addCoverage(grid, { rows: [0], columns: [0], row: 0, column: 0 });
    expect(grid).toEqual([
      [1, 0, 0],
      [0, 0, 6],
    ]);
    expect([0, 1, 4, 5].map(coverageClass)).toEqual(["unvisited", "thin", "thin", "visited"]);
    expect(coverageColor(0, 6)).not.toBe(coverageColor(1, 6));
    expect(coverageColor(6, 6)).not.toBe(coverageColor(5, 60));
  });

  it("overlays a residency grid only on the axes it was binned against", () => {
    const residency: CellResidency = {
      available: true,
      session_id: "s",
      table: "fuel_targets",
      revision: "r",
      reason: null,
      x_axis: [8, 12, 16],
      y_axis: [1200, 2000],
      counts: [
        [0, 0, 0],
        [0, 0, 0],
      ],
      dwell_s: [
        [0, 0, 0],
        [0, 0, 0],
      ],
      samples: 0,
      outside_axes: 0,
    };
    expect(residencyMatchesTable(residency, table)).toBe(true);
    expect(residencyMatchesTable({ ...residency, x_axis: [8, 12, 17] }, table)).toBe(false);
    // The calibration records breakpoints at the two decimals the editor displays;
    // the image's own labels differ by rounding only, and that is the same axis.
    expect(residencyMatchesTable({ ...residency, x_axis: [8.003, 11.998, 16.004] }, table)).toBe(
      true,
    );
    expect(residencyMatchesTable({ ...residency, x_axis: [8.02, 12, 16] }, table)).toBe(false);
    expect(
      residencyMatchesTable(
        {
          available: false,
          session_id: "s",
          table: "fuel_targets",
          reason: "not_captured",
          message: "m",
        },
        table,
      ),
    ).toBe(false);
  });
});

describe("traceGate (R08.8)", () => {
  const rom = { matches_recorded_revision: true, recorded_revision: "R1" };
  const running = {
    registry_revision: "R1",
    calibration_matches_registry: true,
    vehicle_state: { state: "engine_running", derived_at: new Date(now).toISOString() },
  } as unknown as StatusResponse;
  const fresh = { timestamp: now };
  const base = {
    confirmed: true,
    rom,
    status: running,
    now,
    fuelCut: false,
    rpm: fresh,
    bfs: fresh,
  };

  it("passes when every gate holds", () => {
    expect(traceGate(base)).toEqual({ gate: "", trace: "" });
  });

  it("names the first failing gate, in order", () => {
    expect(traceGate({ ...base, confirmed: false }).gate).toMatch(/^Confirm the selected ROM/);
    expect(traceGate({ ...base, rom: { ...rom, matches_recorded_revision: false } }).gate).toMatch(
      /differs from the recorded active revision/,
    );
    expect(
      traceGate({
        ...base,
        status: { ...running, calibration_matches_registry: false, ecu_calibration_id: "X" },
      }).gate,
    ).toMatch(/^ECU reports calibration id X/);
    expect(traceGate({ ...base, status: { ...running, registry_revision: "R2" } }).gate).toMatch(
      /running revision is now R2, not this ROM's R1/,
    );
    const replay = {
      ...running,
      vehicle_state: { ...running.vehicle_state!, state: "replay" },
    } as StatusResponse;
    expect(traceGate({ ...base, status: replay }).gate).toBe(
      "Replay active — live-map tracing is disabled.",
    );
  });

  it("waits for a fresh engine-running status", () => {
    const old = {
      ...running,
      vehicle_state: { ...running.vehicle_state!, derived_at: new Date(now - 16000).toISOString() },
    } as StatusResponse;
    expect(traceGate({ ...base, status: old }).gate).toBe(
      "Waiting for fresh engine-running status.",
    );
    expect(traceGate({ ...base, status: null }).gate).toBe(
      "Waiting for fresh engine-running status.",
    );
  });

  it("keeps live-input reasons out of the shared gate", () => {
    const cut = traceGate({ ...base, fuelCut: true });
    expect(cut.gate).toBe("");
    expect(cut.trace).toMatch(/^Fuel-cut sentinel/);
    expect(traceGate({ ...base, bfs: null }).trace).toMatch(/^Waiting for fresh RPM and BFS/);
    expect(traceGate({ ...base, bfs: { timestamp: now - 751 } }).trace).toMatch(/skew exceeds/);
    expect(traceGate({ ...base, bfs: { timestamp: now - 750 } }).trace).toBe("");
  });

  it("a gate failure is also the trace reason", () => {
    const g = traceGate({ ...base, confirmed: false, fuelCut: true });
    expect(g.trace).toBe(g.gate);
  });
});

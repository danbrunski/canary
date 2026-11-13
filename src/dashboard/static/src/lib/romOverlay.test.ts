import { describe, expect, it } from "vitest";

import { SHA_TUNED, TABLES_ALL_2D, TABLES_WITH_TRIM } from "../test/romLibraryFixtures";
import type { RomTraceContext, TableTrace } from "../types/workbench";
import { overlayTableOf, traceInfoFor } from "./romOverlay";

const trace: TableTrace = {
  x: {
    channel: "mem:ECM_FUEL_TARGET_MAP_X_TRACE",
    axis_mapping: "verified",
    axis_units: "raw",
    scale: 1,
    offset: 0,
    evidence: "firmware",
    request_channel: "mem:fuel_axis",
    stored_decode: { scale: 0.250859, offset: 0 },
  },
  y: {
    channel: "mem:ECM_ENGINE_SPEED_RAM",
    axis_mapping: "verified",
    axis_units: "display",
    scale: 1,
    offset: 0,
    evidence: "rpm",
    request_channel: "mem:spark",
    stored_decode: { scale: 12.5, offset: 0 },
  },
  unavailable_note: null,
  out_of_range: { behaviour: "clamp", evidence: "0x026F40" },
};

const context: RomTraceContext = {
  sha256: SHA_TUNED,
  map_slot: 1,
  afr_scale: 14.7,
  recorded_revision: "ZS44A-20260920",
  matches_recorded_revision: true,
  warnings: [],
  live: { rpm: null, base_fuel_schedule: null, commanded_afr: null },
  tables: [
    {
      kind: "fuel_targets",
      title: "Target AFR",
      x_label: "Base Fuel Schedule",
      x_unit: "ms",
      y_label: "Rotations Per Minute",
      y_unit: "rpm",
      unit: "AFR",
      trace,
    },
    {
      kind: "fuel_compensation",
      title: "Fuel Compensation",
      x_label: "Throttle-model fill",
      x_unit: "%",
      y_label: "Rotations Per Minute",
      y_unit: "rpm",
      unit: "%",
      trace: null,
    },
  ],
};

const find = (id: string, slot: number) => {
  const found = TABLES_ALL_2D.tables.find((table) => table.id === id && table.slot === slot);
  if (!found) throw new Error(`no fixture ${id}@${slot}`);
  return found;
};

describe("overlayTableOf", () => {
  it("matches a library table to its trace by kind, in the slot the definition is for", () => {
    const fuel = find("fuel_targets", 1);
    expect(traceInfoFor(fuel, context)?.kind).toBe("fuel_targets");
    const placed = overlayTableOf(fuel, context);
    expect(placed && "table" in placed).toBe(true);
    if (!placed || !("table" in placed)) return;
    const { table } = placed;
    expect(table.kind).toBe("fuel_targets");
    expect(table.slot).toBe(1);
    // Displayed axes are the image's own labels; the raw axes are its bytes.
    expect(table.x_unit).toBe("ms");
    expect(table.x_axis[0]).toBeCloseTo(8.7627, 3);
    expect(table.y_axis[0]).toBe(1200);
    expect(table.x_axis_raw).toEqual(fuel.axes.cols?.raw);
    expect(table.y_axis_raw).toEqual(fuel.axes.rows?.raw);
    expect(table.trace).toBe(trace);
    expect(table.values).toEqual(fuel.decoded);
  });

  it("does not apply to a copy in another slot, a table with no trace, or an unmapped kind", () => {
    expect(overlayTableOf(find("fuel_targets_maps_2_5", 2), context)).toBeNull();
    expect(overlayTableOf(find("fuel_compensation", 1), context)).toBeNull();
    expect(overlayTableOf(find("timing_trim_a", 1), context)).toBeNull();
    expect(overlayTableOf(find("base_timing_rate_map", 1), context)).toBeNull();
    expect(traceInfoFor(find("fuel_compensation", 1), context)).toBeNull();
  });

  it("refuses an axis that is not in the units the lookup compares, naming both", () => {
    const fuel = TABLES_WITH_TRIM.tables[0];
    const raw = {
      ...fuel,
      axes: { ...fuel.axes, cols: fuel.axes.cols && { ...fuel.axes.cols, editor_labels: null } },
    };
    const placed = overlayTableOf(raw, context);
    expect(placed && "refusal" in placed ? placed.refusal : "").toMatch(
      /needs Base Fuel Schedule in ms on the columns.*labels them raw counts and rpm/,
    );
  });

  it("refuses a table with no decoded values or no axes rather than placing a cell", () => {
    const fuel = find("fuel_targets", 1);
    const undecoded = overlayTableOf({ ...fuel, decoded: null }, context);
    expect(undecoded && "refusal" in undecoded ? undecoded.refusal : "").toMatch(
      /no decoded values/,
    );
    const hole = overlayTableOf(
      {
        ...fuel,
        decoded: (fuel.decoded ?? []).map((row, r) =>
          row.map((v, c) => (r === 0 && c === 0 ? null : v)),
        ),
      },
      context,
    );
    expect(hole && "refusal" in hole ? hole.refusal : "").toMatch(/no value/);
  });
});

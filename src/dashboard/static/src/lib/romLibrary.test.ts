import { describe, expect, it } from "vitest";

import { HttpError, NetworkError } from "../api/core";
import { ENTRY_K106, ENTRY_TUNED, REGIONS, TABLES, readyStatus } from "../test/romLibraryFixtures";
import {
  backupPreconditions,
  backupStartBlocker,
  downloadName,
  absoluteRegions,
  eraseSectorSummary,
  formatBytes,
  formatCell,
  hexAddress,
  hexSpan,
  isFuelTargets,
  mapTableOf,
  otherBackupRefusals,
  refusalText,
  regionsOverlapping,
  romFileProblem,
  slotRegionName,
} from "./romLibrary";

function sized(name: string, size: number) {
  const file = new File([""], name);
  Object.defineProperty(file, "size", { value: size });
  return file;
}

describe("romLibrary formatting", () => {
  it("formats addresses, spans and sizes", () => {
    expect(hexAddress(0xa169)).toBe("0x00A169");
    expect(hexSpan(0x9598, 0x95a3)).toBe("0x009598–0x0095A3");
    expect(hexSpan(0xe025c, 0xe025c)).toBe("0x0E025C");
    expect(formatBytes(16)).toBe("16 B");
    expect(formatBytes(24 * 1024)).toBe("24 KiB");
    expect(formatBytes(1048576)).toBe("1 MiB");
    expect(formatBytes(262143)).toBe("256.0 KiB");
  });

  it("summarises erase sectors as runs", () => {
    const sectors = [
      { index: 9, start: 0x20000, end: 0x3ffff },
      ...Array.from({ length: 8 }, (_, i) => ({ index: i, start: i * 4096, end: i * 4096 + 4095 })),
      { index: 8, start: 0x8000, end: 0x1ffff },
      { index: 10, start: 0x40000, end: 0x5ffff },
    ];
    expect(eraseSectorSummary(sectors)).toBe("8 × 4 KiB, 1 × 96 KiB, 2 × 128 KiB");
  });

  it("orders overlapping regions enclosing-first", () => {
    const regions = [
      { start: 0x8204, end: 0x8219, name: "Calibration ID" },
      { start: 0x8000, end: 0x11fff, name: "Calibration" },
      { start: 0x20000, end: 0x20003, name: "Header" },
    ].map((region) => ({
      ...region,
      kind: "calibration" as const,
      confidence: "verified" as const,
      added_by_uprev: false,
      evidence: "",
    }));
    expect(regionsOverlapping(regions, 0x8000, 0x8fff).map((region) => region.name)).toEqual([
      "Calibration",
      "Calibration ID",
    ]);
  });

  it("names downloads <title>-<sha8>.bin, never .OsirisROM", () => {
    expect(downloadName(ENTRY_TUNED)).toBe("Running_tune-a46baa24.bin");
    expect(downloadName(ENTRY_K106)).toBe("k106-bbbbbbbb.bin");
    expect(downloadName({ ...ENTRY_K106, label: null, filename: "ZS44A Tuned.OsirisROM" })).toBe(
      "ZS44A_Tuned-bbbbbbbb.bin",
    );
  });
});

describe("romFileProblem", () => {
  it("accepts .bin and .OsirisROM up to 2 MiB", () => {
    expect(romFileProblem(sized("read.bin", 1048576))).toBeNull();
    expect(romFileProblem(sized("ZS44A.OsirisROM", 800_000))).toBeNull();
    // The encrypted backup passes here; the server refuses it by name.
    expect(romFileProblem(sized("backup.bin", 1048592))).toBeNull();
  });

  it("names the problem otherwise", () => {
    expect(romFileProblem(sized("rom.hex", 10))).toMatch(/choose a plain image/);
    expect(romFileProblem(sized("rom.bin", 0))).toBe("rom.bin is empty.");
    expect(romFileProblem(sized("rom.bin", 2 * 1024 * 1024 + 1))).toMatch(/2 MiB/);
  });
});

describe("refusalText", () => {
  it("returns a string detail verbatim and untruncated", () => {
    const detail = "x".repeat(300);
    expect(refusalText(new HttpError(422, JSON.stringify({ detail })))).toBe(detail);
  });

  it("reads an object detail's reason and a validation list's messages", () => {
    expect(
      refusalText(new HttpError(409, JSON.stringify({ detail: { reason: "capture active" } }))),
    ).toBe("capture active");
    expect(
      refusalText(
        new HttpError(422, JSON.stringify({ detail: [{ msg: "a" }, { msg: "b" }, { loc: 1 }] })),
      ),
    ).toBe("a; b");
  });

  it("keeps a 413/415 reason and a plain-text body", () => {
    expect(refusalText(new HttpError(413, '{"detail":"ROM exceeds 2 MiB limit"}'))).toBe(
      "ROM exceeds 2 MiB limit",
    );
    expect(refusalText(new HttpError(415, "Send octet-stream"))).toBe("Send octet-stream");
  });

  it("leaves auth, server and network failures to describeError", () => {
    expect(refusalText(new HttpError(401, '{"detail":"bad token"}'))).toMatch(/Not authorized/);
    expect(refusalText(new HttpError(500, "boom"))).toBe("Server error 500.");
    expect(refusalText(new NetworkError("/control/rom/library"))).toMatch(/Can't reach the daemon/);
  });
});

describe("backup preconditions", () => {
  it("all ok when key on, engine off, passive off, no tester, no capture", () => {
    expect(backupPreconditions(readyStatus()).map((item) => item.state)).toEqual([
      "ok",
      "ok",
      "ok",
      "ok",
    ]);
    expect(backupStartBlocker("t", readyStatus(), null)).toBeNull();
  });

  it("a foreign tester heard earlier but no longer holding the ECM does not block", () => {
    const status = readyStatus({
      passive: {
        ...readyStatus().passive!,
        foreign_tester: {
          tester_address: "F1",
          module_address: "10",
          last_heard_at: "2026-10-04T14:50:00Z",
          seconds_since_heard: 600,
        },
      },
    });
    const foreign = backupPreconditions(status).find((item) => item.key === "foreign")!;
    expect(foreign.state).toBe("ok");
    expect(foreign.value).toBe("none active, last heard 10 min ago");
  });

  it("each refusal blocks with its own fix", () => {
    const engine = readyStatus({
      vehicle_state: { ...readyStatus().vehicle_state!, state: "engine_running" },
    });
    expect(backupStartBlocker("t", engine, null)).toMatch(/engine is running/);
    const passive = readyStatus({
      passive: { ...readyStatus().passive!, enabled: true, source: "mcp" },
    });
    expect(backupStartBlocker("t", passive, null)).toBe(
      "Passive mode is armed by mcp. Disarm it first.",
    );
    const foreign = readyStatus({
      passive: { ...readyStatus().passive!, ecm_session_claims: { foreign: 3 } },
    });
    expect(backupStartBlocker("t", foreign, null)).toMatch(/Another tester holds the ECM/);
    expect(backupStartBlocker("", readyStatus(), null)).toMatch(/control token/);
    expect(backupStartBlocker("t", readyStatus(), { state: "running" })).toBe(
      "A backup is already running.",
    );
    expect(backupStartBlocker("t", null, null)).toMatch(/Waiting for \/status/);
  });

  it("the daemon's check overrides a green chip and names its reason", () => {
    // /status shows no foreign claim, but the daemon still refuses: a tester
    // was heard inside its quiet period.
    const checks = [
      { id: "vehicle_state", ok: true, detail: "ignition_only (key on, engine off)" },
      {
        id: "foreign_tester",
        ok: false,
        detail:
          "another tester (K-line F1 -> 10) was heard 12 s ago; wait until it has been quiet 60 s",
      },
      { id: "passive", ok: true, detail: "passive mode is off" },
    ];
    const foreign = backupPreconditions(readyStatus(), checks).find(
      (item) => item.key === "foreign",
    )!;
    expect(foreign.state).toBe("blocked");
    expect(foreign.value).toBe("refused by the daemon");
    expect(
      backupStartBlocker("t", readyStatus(), { state: "idle", preconditions: checks }),
    ).toMatch(/was heard 12 s ago/);
  });

  it("refusals no chip covers come from can_start/blocked_by and are listed separately", () => {
    const checks = [
      {
        id: "engine_watch",
        ok: false,
        detail: "no fresh broadcast engine speed: an engine start could not be seen",
      },
      { id: "capture", ok: true, detail: "no capture active" },
    ];
    expect(otherBackupRefusals(checks).map((check) => check.id)).toEqual(["engine_watch"]);
    expect(otherBackupRefusals(null)).toEqual([]);
    expect(
      backupStartBlocker("t", readyStatus(), {
        state: "idle",
        preconditions: checks,
        can_start: false,
        blocked_by: checks[0].detail,
      }),
    ).toBe(checks[0].detail);
  });

  it("an absent vehicle_state or transmit gate is unknown, and unknown blocks", () => {
    const bare = readyStatus({ vehicle_state: null, passive: null });
    const states = backupPreconditions(bare).map((item) => item.state);
    expect(states).toEqual(["unknown", "unknown", "unknown", "ok"]);
    expect(backupStartBlocker("t", bare, null)).toBe("The vehicle state is not reported yet.");
  });
});

describe("regions", () => {
  it("separates slot spans from absolute regions and strips the slot prefix", () => {
    const absolute = absoluteRegions(REGIONS);
    expect(absolute.map((region) => region.name)).toEqual([
      "Boot code",
      "Calibration constants and tables",
      "Calibration ID",
      "UpRev map slots 1–5",
    ]);
    const span = REGIONS.find((region) => region.slot === 2)!;
    expect(span.name).toBe("Map 2: Slot header or parameters");
    expect(slotRegionName(span)).toBe("Slot header or parameters");
  });
});

describe("tables", () => {
  it("refuses MapTable for a table with a valueless cell, and shows it as a dash", () => {
    const [fuel] = TABLES.tables;
    const decoded = fuel.decoded!.map((row) => [...row]);
    decoded[0][0] = null;
    expect(mapTableOf({ ...fuel, decoded })).toBeNull();
    expect(formatCell(null)).toBe("—");
    expect(formatCell(28715)).toBe("28,715");
    expect(formatCell(11.5435)).toBe("11.54");
  });

  it("draws only Fuel Targets with MapTable, mapping rows to rpm and columns to BFS", () => {
    const [fuel, k] = TABLES.tables;
    expect(isFuelTargets(fuel)).toBe(true);
    expect(isFuelTargets(k)).toBe(false);
    expect(isFuelTargets({ ...k, kind: undefined, name: "Fuel_Targets" })).toBe(true);
    const table = mapTableOf(fuel)!;
    expect(table.y_axis[0]).toBe(1200);
    // The BFS axis has no declared scale, so it is drawn in raw counts.
    expect(table.x_axis[0]).toBe(52);
    expect(table.x_unit).toBe("");
    expect(table.values[0][0]).toBeCloseTo(14.7, 2);
    expect(table.raw[0][0]).toBe(128);
    expect(mapTableOf({ ...fuel, decoded: null })).toBeNull();
  });
});

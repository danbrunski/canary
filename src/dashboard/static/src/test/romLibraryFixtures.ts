// Shared payloads for the ROM Library page tests (W08.6, W08.8), in the
// shapes declared by types/romLibrary. Synthetic: no ROM bytes, only the
// layout facts the 2026-10-04 map records.

import type { StatusResponse } from "../types";
import type {
  RomBackupState,
  RomBlock,
  RomDescription,
  RomLibraryEntry,
  RomRegion,
  RomTablesResponse,
} from "../types/romLibrary";

export const SHA_TUNED = "a46baa24".padEnd(60, "0") + "be50";
export const SHA_K106 = "b".repeat(64);

export const ENTRY_TUNED: RomLibraryEntry = {
  sha256: SHA_TUNED,
  crc32: "95b993c5",
  container_sha256: "c".repeat(64),
  source: "upload_osirisrom",
  sources: ["upload_osirisrom", "ecu_read"],
  filename: "ZS44A-Tuned-20260920.OsirisROM",
  label: "Running tune",
  added_at: "2026-10-04T15:00:00Z",
  calibration_id: "1ZS44A00000",
  checksum: {
    ok: true,
    stored: { sum: "1a2b3c4d", xor: "5e6f7a8b" },
    computed: { sum: "1a2b3c4d", xor: "5e6f7a8b" },
  },
  revision_match: { revision: "ZS44A-20260920", status: "matched" },
  size: 1048576,
  read_meta: null,
};

export const ENTRY_K106: RomLibraryEntry = {
  ...ENTRY_TUNED,
  sha256: SHA_K106,
  crc32: "0badf00d",
  container_sha256: null,
  source: "upload_plain",
  sources: ["upload_plain"],
  filename: "k106.bin",
  label: null,
  added_at: "2026-10-03T15:00:00Z",
  revision_match: { revision: null, status: "unrecognised" },
};

/** 256 blocks: dense boot code, erased tail of the boot block, calibration
 *  data, application code, the UpRev slots and the erased top. */
export const BLOCKS: RomBlock[] = Array.from({ length: 256 }, (_, index) => {
  const offset = index * 4096;
  const cls: RomBlock["class"] =
    index < 6
      ? "dense"
      : index < 8
        ? "erased"
        : index < 17
          ? "data"
          : index < 32
            ? "erased"
            : index < 224
              ? "dense"
              : index < 227
                ? "sparse"
                : "erased";
  return {
    offset,
    ff_pct: cls === "erased" ? 100 : cls === "sparse" ? 54 : 3,
    entropy: cls === "erased" ? 0 : cls === "data" ? 5.2 : 6.9,
    class: cls,
  };
});

/** A map-slot span resolved into slot `slot`, as the daemon lists it. */
function slotSpan(
  slot: number,
  start: number,
  end: number,
  name: string,
  confidence: RomRegion["confidence"],
  evidence: string,
): RomRegion {
  const base = 0x0e0000 + (slot - 1) * 0x800;
  return {
    start: base + start,
    end: base + end,
    size: end - start + 1,
    name: `Map ${slot}: ${name}`,
    kind: "uprev",
    confidence,
    added_by_uprev: true,
    evidence,
    slot,
  };
}

export const REGIONS: RomRegion[] = [
  {
    start: 0x000000,
    end: 0x005fff,
    name: "Boot code",
    kind: "boot",
    confidence: "unknown",
    added_by_uprev: false,
    evidence: "Dense, code-like bytes; not disassembled.",
  },
  {
    start: 0x008000,
    end: 0x011fff,
    name: "Calibration constants and tables",
    kind: "calibration",
    confidence: "unknown",
    added_by_uprev: false,
    evidence: "Low-entropy bytes typical of tables.",
  },
  {
    start: 0x008204,
    end: 0x008219,
    name: "Calibration ID",
    kind: "calibration",
    confidence: "verified",
    added_by_uprev: false,
    evidence: "ASCII in the image; the ID the tune registry records.",
  },
  {
    start: 0x0e0000,
    end: 0x0e27ff,
    name: "UpRev map slots 1–5",
    kind: "uprev",
    confidence: "verified",
    added_by_uprev: true,
    evidence: "The target table appears at +0x220 in all five slots.",
  },
  ...[1, 2].flatMap((slot) => [
    slotSpan(
      slot,
      0x000,
      0x09b,
      "Slot header or parameters",
      "unknown",
      "Bytes seen; meaning not decoded.",
    ),
    slotSpan(
      slot,
      0x09c,
      0x0bf,
      "Timing-like cells",
      "inferred",
      "k106 added spark at 3000-4500 high load; these cells went up one count.",
    ),
  ]),
];

export const DESCRIPTION: RomDescription = {
  sha256: SHA_TUNED,
  crc32: "95b993c5",
  container_sha256: "c".repeat(64),
  calibration_id: "1ZS44A00000",
  size: 1048576,
  checksum: ENTRY_TUNED.checksum,
  revision_match: ENTRY_TUNED.revision_match,
  blocks: BLOCKS,
  regions: REGIONS,
  tables: [],
  map_slots: {
    base: 0x0e0000,
    stride: 0x800,
    count: 5,
    end: 0x0e27ff,
    confidence: "verified",
    evidence: "The target table appears at +0x220 in all five slots.",
  },
  map: {
    definition: "rom_map.yaml",
    calibration_id: "1ZS44A00000",
    calibration_ids: ["1ZS44A00000"],
    image_size: 1048576,
    erase_sectors: [
      ...Array.from({ length: 8 }, (_, i) => ({
        index: i,
        start: i * 4096,
        end: i * 4096 + 4095,
      })),
      { index: 8, start: 0x8000, end: 0x1ffff },
      ...Array.from({ length: 7 }, (_, i) => ({
        index: 9 + i,
        start: 0x20000 + i * 0x20000,
        end: 0x3ffff + i * 0x20000,
      })),
    ],
  },
  map_reason: null,
};

// Shapes as `GET /control/rom/library/{sha}/tables` returns them for the
// Xterra map (W08.4): the rpm axis decodes (raw x 50), the BFS axis has no
// declared scale and comes back raw, and K is `k_constant`, raw only.
const RPM = [1200, 1600, 2000, 2800, 3600, 4000, 4400, 5200];
const BFS_RAW = [52, 68, 93, 100, 108, 110, 127, 128];
const RAW = RPM.map((_, r) => BFS_RAW.map((_, c) => 128 + r * 3 + c));
const FUEL_DECODE = {
  formula: "afr = 1881.6 / raw",
  kind: "reciprocal" as const,
  quantity: "afr",
  unit: "AFR",
  confidence: "verified" as const,
  evidence: "AFR = 14.7 x 128 / raw reproduces every transcribed AFR in afr_calibration.yaml.",
};
const FUEL_AXES = {
  rows: {
    id: "fuel_targets_rpm_axis",
    name: "Fuel Targets rpm axis",
    offset: 0xcaa5,
    raw: RPM.map((rpm) => rpm / 50),
    decoded: RPM,
    unit: "rpm",
    decode: {
      formula: "rpm = raw × 50",
      kind: "linear" as const,
      quantity: "rpm",
      unit: "rpm",
      confidence: "verified" as const,
      evidence: "raw x 50 reproduces the transcribed rpm breakpoints.",
    },
    confidence: "verified" as const,
  },
  cols: {
    id: "fuel_targets_bfs_axis",
    name: "Fuel Targets BFS axis",
    offset: 0xc894,
    raw: BFS_RAW,
    decoded: null,
    unit: null,
    decode: null,
    confidence: "verified" as const,
  },
};

export const TABLES: RomTablesResponse = {
  sha256: SHA_TUNED,
  tables: [
    {
      id: "fuel_targets",
      name: "Fuel Targets",
      label: "Fuel Targets (Map 1)",
      slot: 1,
      kind: "fuel_targets",
      role: "table",
      decode: FUEL_DECODE,
      offset: 0x0e0220,
      end: 0x0e025f,
      rows: 8,
      cols: 8,
      dtype: "u8",
      unit: "AFR",
      raw: RAW,
      decoded: RAW.map((row) => row.map((raw) => 1881.6 / raw)),
      axes: FUEL_AXES,
      confidence: "verified",
      evidence: "Two known answers: the transcription and the 09-20 one-cell edit.",
    },
    {
      id: "k_constant",
      name: "K (injector constant)",
      label: "K (injector constant) (Map 1)",
      slot: 1,
      kind: "k_constant",
      role: "table",
      decode: null,
      offset: 0x0e0762,
      end: 0x0e0763,
      rows: 1,
      cols: 1,
      dtype: "be16",
      unit: null,
      raw: [[28715]],
      decoded: null,
      axes: { rows: null, cols: null },
      confidence: "inferred",
      evidence: "28,715 / 27,089 = 1.0600, and k106's note says K 1.06.",
    },
    {
      id: "fuel_targets_maps_2_5",
      name: "Fuel Targets",
      label: "Fuel Targets (Map 2)",
      slot: 2,
      kind: "fuel_targets",
      role: "table",
      decode: FUEL_DECODE,
      offset: 0x0e0a20,
      end: 0x0e0a5f,
      rows: 8,
      cols: 8,
      dtype: "u8",
      unit: "AFR",
      raw: RAW,
      decoded: RAW.map((row) => row.map((raw) => 1881.6 / raw)),
      axes: FUEL_AXES,
      confidence: "inferred",
      evidence: "In the UpRev base all five slots hold this table; no Map 2-5 edit confirmed it.",
    },
  ],
};

export function backupState(overrides: Partial<RomBackupState> = {}): RomBackupState {
  return {
    state: "idle",
    reason: null,
    blocks_done: 0,
    blocks_total: 0,
    bytes: 0,
    elapsed_s: 0,
    eta_s: null,
    retries: 0,
    ...overrides,
  };
}

/** `/status` with every backup precondition met: key on, engine off,
 *  passive off, no foreign tester, no capture. */
export function readyStatus(overrides: Partial<StatusResponse> = {}): StatusResponse {
  return {
    vehicle_state: {
      state: "ignition_only",
      rpm: 0,
      coolant_temp: 40,
      run_time: 0,
      obd_freshness_seconds: 0.5,
      can_freshness_seconds: 0.1,
      replay_id: null,
      derived_at: "2026-10-04T15:00:00Z",
    },
    passive: {
      enabled: false,
      source: null,
      suppressed_frames: 0,
      ttl_remaining_seconds: null,
      ecm_session_owner: "can_diagnostics",
      ecm_session_claims: {},
      tx_suppressed: false,
      foreign_tester: null,
      survives_restart: false,
    },
    capture_profile: null,
    ...overrides,
  } as StatusResponse;
}

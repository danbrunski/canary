// Pure helpers for the ROM Library page (W08.6, W08.8): address and size
// formatting, block and region lookups, the backup preconditions read off
// `/status`, and the bridge from a library table to `MapTable`.

import { HttpError } from "../api/http";
import { describeError } from "../hooks/useControlResource";
import type { StatusResponse } from "../types";
import type {
  RomBackupCheck,
  RomBackupState,
  RomBlock,
  RomBlockClass,
  RomEditorDisagreement,
  RomEraseSector,
  RomLibraryEntry,
  RomRegion,
  RomSource,
  RomTableAxis,
  RomTableValues,
} from "../types/romLibrary";
import type { RomTable } from "../types/workbench";

/** The statistics granularity (W08.2) and the grid's row width: 16 blocks of
 *  4 KiB make one 64 KiB row. */
export const BLOCK_SIZE = 4096;
export const BLOCKS_PER_ROW = 16;

/** The library's upload cap (W08.3); the server enforces it too. */
export const ROM_UPLOAD_LIMIT = 2 * 1024 * 1024;

const ROM_EXTENSIONS = [".bin", ".osirisrom"];

/** `0x00A169`. Six digits covers images up to 16 MiB. */
export function hexAddress(value: number, width = 6): string {
  return `0x${value.toString(16).toUpperCase().padStart(width, "0")}`;
}

/** `start–end` (inclusive end), or the single address for a one-byte span. */
export function hexSpan(start: number, end: number): string {
  return start === end ? hexAddress(start) : `${hexAddress(start)}–${hexAddress(end)}`;
}

/** `16 B`, `24 KiB`, `1 MiB`; non-whole KiB to one decimal. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes >= 1024 * 1024 && bytes % (1024 * 1024) === 0) return `${bytes / (1024 * 1024)} MiB`;
  const kib = bytes / 1024;
  return Number.isInteger(kib) ? `${kib} KiB` : `${kib.toFixed(1)} KiB`;
}

/** What a block's class says about its bytes. Statistics, not meaning; an
 *  unrecognised class is shown as sent. */
export const BLOCK_CLASS_TEXT: Record<RomBlockClass, string> = {
  dense: "dense: code, or packed tables",
  data: "low-entropy data: constants and tables",
  sparse: "mostly erased, some data",
  erased: "erased (all 0xFF)",
};

export function blockClassText(cls: string): string {
  return cls in BLOCK_CLASS_TEXT ? BLOCK_CLASS_TEXT[cls as RomBlockClass] : cls;
}

/** Regions overlapping `[start, end]`, the enclosing ones first. */
export function regionsOverlapping(regions: RomRegion[], start: number, end: number): RomRegion[] {
  return regions
    .filter((region) => region.start <= end && region.end >= start)
    .sort((a, b) => b.end - b.start - (a.end - a.start) || a.start - b.start);
}

/** Regions sorted for the table: by address, an enclosing region before the
 *  entries inside it. */
export function sortRegions(regions: RomRegion[]): RomRegion[] {
  return [...regions].sort((a, b) => a.start - b.start || b.end - a.end);
}

/** The blocks of one 64 KiB grid row. */
export function gridRows(blocks: RomBlock[]): RomBlock[][] {
  const rows: RomBlock[][] = [];
  for (let index = 0; index < blocks.length; index += BLOCKS_PER_ROW) {
    rows.push(blocks.slice(index, index + BLOCKS_PER_ROW));
  }
  return rows;
}

/** `8 × 4 KiB, 1 × 96 KiB, 7 × 128 KiB`: runs of equal-sized sectors. */
export function eraseSectorSummary(sectors: RomEraseSector[]): string {
  const runs: { size: number; count: number }[] = [];
  for (const sector of [...sectors].sort((a, b) => a.start - b.start)) {
    const size = sector.end - sector.start + 1;
    const last = runs[runs.length - 1];
    if (last && last.size === size) last.count += 1;
    else runs.push({ size, count: 1 });
  }
  return runs.map((run) => `${run.count} × ${formatBytes(run.size)}`).join(", ");
}

export const SOURCE_TEXT: Record<RomSource, string> = {
  upload_plain: "uploaded image",
  upload_osirisrom: "uploaded .OsirisROM",
  ecu_read: "ECU read",
};

/** Every source an entry arrived by, deduplicated, oldest first. */
export function entrySources(entry: RomLibraryEntry): RomSource[] {
  const all = [entry.source, ...(entry.sources ?? [])];
  return [...new Set(all.filter((source): source is RomSource => source !== null))];
}

/** Regions at absolute addresses: the region table. Map-slot spans (with
 *  `slot` set) are listed per slot with the tables instead. */
export function absoluteRegions(regions: RomRegion[]): RomRegion[] {
  return regions.filter((region) => region.slot == null);
}

/** A slot span's name without the `Map <n>: ` prefix the daemon adds when it
 *  resolves the span per slot. */
export function slotRegionName(region: RomRegion): string {
  const prefix = `Map ${region.slot}: `;
  return region.name.startsWith(prefix) ? region.name.slice(prefix.length) : region.name;
}

/** The name the list shows: label, else filename, else calibration ID, else
 *  the hash prefix. */
export function entryTitle(entry: RomLibraryEntry): string {
  return (
    entry.label?.trim() ||
    entry.filename ||
    (entry.calibration_id ? `${entry.calibration_id} image` : `image ${entry.sha256.slice(0, 12)}`)
  );
}

/** `<title>-<sha8>.bin`, filesystem-safe. Downloads are plain images only
 *  (W08 pre-made decision), so the extension is always `.bin`. */
export function downloadName(entry: RomLibraryEntry): string {
  const stem = entryTitle(entry)
    .replace(/\.(bin|osirisrom)$/i, "")
    .replace(/[^A-Za-z0-9._-]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return `${stem || "rom"}-${entry.sha256.slice(0, 8)}.bin`;
}

/** Why a picked file cannot be sent, or null. The server detects the format
 *  from the bytes and refuses by name; this only stops the obvious misses
 *  before uploading them. */
export function romFileProblem(file: File): string | null {
  const name = file.name.toLowerCase();
  if (!ROM_EXTENSIONS.some((extension) => name.endsWith(extension)))
    return `${file.name}: choose a plain image (.bin) or an .OsirisROM.`;
  if (!file.size) return `${file.name} is empty.`;
  if (file.size > ROM_UPLOAD_LIMIT) return `${file.name} is larger than the 2 MiB upload limit.`;
  return null;
}

/** FastAPI puts a refusal's reason in `detail`: a string, or an object
 *  carrying `reason`/`message`, or a validation list. */
function detailText(body: string): string | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    return body.trim() || null;
  }
  if (typeof parsed !== "object" || parsed === null || !("detail" in parsed))
    return body.trim() || null;
  const detail: unknown = parsed.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item: unknown) =>
        typeof item === "object" && item !== null && "msg" in item && typeof item.msg === "string"
          ? item.msg
          : null,
      )
      .filter((item): item is string => item !== null);
    return messages.length ? messages.join("; ") : null;
  }
  if (typeof detail === "object" && detail !== null) {
    for (const key of ["reason", "message"] as const) {
      const value: unknown = (detail as Record<string, unknown>)[key];
      if (typeof value === "string") return value;
    }
  }
  return null;
}

/** The server's refusal reason, verbatim and untruncated, for any 4xx but an
 *  auth failure: these routes refuse by name (the encrypted UpRev backup, the
 *  2 MiB cap, a backup precondition), and `describeError` drops the reason of
 *  a 413/415. Everything else is `describeError`'s text. */
export function refusalText(err: unknown): string {
  if (
    err instanceof HttpError &&
    err.status >= 400 &&
    err.status < 500 &&
    err.status !== 401 &&
    err.status !== 403
  ) {
    const detail = detailText(err.body);
    if (detail) return detail;
  }
  return describeError(err);
}

/** The on-screen warning for a table whose axes a tuning editor labels
 *  differently from the ECM's code. Shared by the ROM Library and the ROM Editor,
 *  so both say the same thing: whether a cell may be matched by position,
 *  and how to read the editor's labels. */
export function describeEditorDisagreement(d: RomEditorDisagreement): string {
  const positions = d.positions_match
    ? `Grid positions agree: row i, column j is the same cell in ${d.editor} and here. Its axis labels do not.`
    : `Grid positions differ: do not match cells between ${d.editor} and here by position.`;
  return (
    `${d.editor} labels these axes differently: rows = ${d.editor_row_labels}; ` +
    `columns = ${d.editor_col_labels}. ${positions} ${d.consequence} ${d.translation}`
  );
}

/** True for the Fuel Targets table, which the page draws with `MapTable`. */
export function isFuelTargets(table: RomTableValues): boolean {
  if (table.kind) return table.kind === "fuel_targets";
  return (
    table.name
      .trim()
      .toLowerCase()
      .replace(/[\s_]+/g, " ") === "fuel targets"
  );
}

function complete(values: (number | null)[]): values is number[] {
  return values.every((value) => value !== null);
}

/** How an axis's breakpoints are shown: the editor's labels where the map
 *  declares them (UpRev's BFS in ms), else the decode, else the raw bytes. */
export interface AxisBreakpoints {
  label: string;
  unit: string;
  values: (number | null)[];
}

export function axisBreakpoints(axis: RomTableAxis): AxisBreakpoints {
  const labels = axis.editor_labels;
  if (labels?.values) {
    return {
      label: `${axis.name}, ${labels.editor}'s ${labels.quantity}`,
      unit: labels.unit,
      values: labels.values,
    };
  }
  return {
    label: axis.name,
    unit: axis.decoded ? (axis.unit ?? "") : "",
    values: axis.decoded ?? axis.raw,
  };
}

/** A library table as `MapTable` draws it, or null when it lacks a decode,
 *  an axis, or a value in any cell (it is then shown raw). */
export function mapTableOf(table: RomTableValues): RomTable | null {
  const rows = table.axes.rows;
  const cols = table.axes.cols;
  if (!rows || !cols || !table.decoded) return null;
  const values = table.decoded.map((row) => (complete(row) ? row : null));
  const x = axisBreakpoints(cols);
  const y = axisBreakpoints(rows);
  const xAxis = x.values;
  const yAxis = y.values;
  if (!complete(xAxis) || !complete(yAxis) || values.some((row) => row === null)) return null;
  return {
    slot: table.slot ?? 0,
    kind: "fuel_targets",
    title: table.name,
    x_label: x.label,
    x_unit: x.unit,
    y_label: y.label,
    y_unit: y.unit,
    unit: table.unit ?? "",
    status: "available",
    reason: null,
    x_axis: xAxis,
    y_axis: yAxis,
    values: values as number[][],
    raw: table.raw,
    trace: null,
  };
}

/** The slots the tables payload covers, ascending. */
export function tableSlots(tables: RomTableValues[]): number[] {
  return [
    ...new Set(tables.map((table) => table.slot).filter((slot): slot is number => slot !== null)),
  ].sort((a, b) => a - b);
}

/** Bytes per cell for a table dtype. */
export function cellBytes(table: RomTableValues): number {
  return table.dtype === "be16" ? 2 : 1;
}

/** Integers as integers, anything else to two decimals. */
export function formatCell(value: number | null): string {
  if (value === null) return "—";
  return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2);
}

export type PreconditionState = "ok" | "blocked" | "unknown";

export interface BackupPrecondition {
  key: "vehicle" | "passive" | "foreign" | "capture";
  label: string;
  state: PreconditionState;
  /** The chip's value text. */
  value: string;
  /** What blocks the read and how to clear it; null when nothing does. */
  fix: string | null;
}

const VEHICLE_FIX: Record<string, string> = {
  engine_running: "The engine is running. Switch it off and leave the key on.",
  engine_off: "The ECM is not answering. Turn the key to on, engine off.",
  parked: "The vehicle is asleep. Turn the key to on, engine off.",
  bench: "No vehicle is on the OBD port. Plug the Pi into the vehicle.",
  no_data: "Neither bus is fresh and no 12 V is read. Check the Pi is connected to the vehicle.",
  replay: "A replay is running. Stop it first.",
  passive:
    "CANary is not transmitting, so the ECM state cannot be confirmed. Disarm passive mode first.",
  unknown: "The vehicle state is not classified. Wait for a clean reading with the key on.",
};

function secondsAgo(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

/** The daemon's check id behind each `/status` chip. */
const SERVER_CHECK: Record<BackupPrecondition["key"], string> = {
  vehicle: "vehicle_state",
  passive: "passive",
  foreign: "foreign_tester",
  capture: "capture",
};

/** The W08.7 refusals that `/status` can show before Start is pressed, merged
 *  with the daemon's own checks from `GET /control/rom/backup` when given: a
 *  chip is blocked when either side blocks, and then names the daemon's
 *  reason. The daemon is the authority (its foreign-tester rule includes a
 *  quiet period `/status` cannot show), so a chip never reads green where
 *  Start would be refused. "Unknown" blocks too: a precondition that cannot
 *  be read is not met. */
export function backupPreconditions(
  status: StatusResponse | null,
  checks: RomBackupCheck[] | null = null,
): BackupPrecondition[] {
  const vehicle = status?.vehicle_state?.state;
  const gate = status?.passive;
  const capture = status?.capture_profile;
  const heard = gate?.foreign_tester ?? null;
  const foreignHolds =
    gate?.ecm_session_owner === "foreign" || (gate ? "foreign" in gate.ecm_session_claims : false);
  const tester = heard?.tester_address ? ` (tester ${heard.tester_address})` : "";
  const local: BackupPrecondition[] = [
    {
      key: "vehicle",
      label: "Vehicle",
      state: !vehicle ? "unknown" : vehicle === "ignition_only" ? "ok" : "blocked",
      value: vehicle ?? "not reported",
      fix: !vehicle
        ? "The vehicle state is not reported yet."
        : vehicle === "ignition_only"
          ? null
          : (VEHICLE_FIX[vehicle] ?? `Vehicle state ${vehicle}: key on, engine off is required.`),
    },
    {
      key: "passive",
      label: "Passive",
      state: !gate ? "unknown" : gate.enabled ? "blocked" : "ok",
      value: !gate ? "not reported" : gate.enabled ? "armed" : "off",
      fix: !gate
        ? "The transmit gate is not reported."
        : gate.enabled
          ? `Passive mode is armed${gate.source ? ` by ${gate.source}` : ""}. Disarm it first.`
          : null,
    },
    {
      key: "foreign",
      label: "Foreign tester",
      state: !gate ? "unknown" : foreignHolds ? "blocked" : "ok",
      value: !gate
        ? "not reported"
        : foreignHolds
          ? "holds the ECM"
          : heard
            ? `none active, last heard ${secondsAgo(heard.seconds_since_heard)}`
            : "none heard",
      fix: !gate
        ? "The transmit gate is not reported."
        : foreignHolds
          ? `Another tester holds the ECM session${tester}${heard ? `, heard ${secondsAgo(heard.seconds_since_heard)}` : ""}. Disconnect it first.`
          : null,
    },
    {
      key: "capture",
      label: "Capture",
      state: !status ? "unknown" : capture?.active ? "blocked" : "ok",
      value: !status ? "not reported" : capture?.active ? capture.name : "none",
      fix: !status
        ? "Status is not reported."
        : capture?.active
          ? `Capture '${capture.name}' is recording, and the read pauses all polling. Stop the capture first.`
          : null,
    },
  ];
  return local.map((item) => {
    const server = checks?.find((check) => check.id === SERVER_CHECK[item.key]);
    if (!server || server.ok) return item;
    return {
      ...item,
      state: "blocked",
      value: item.state === "blocked" ? item.value : "refused by the daemon",
      fix: server.detail,
    };
  });
}

/** The daemon's failing checks that no `/status` chip covers (recipe, OBD
 *  source, engine watch, ECM session, another backup). */
export function otherBackupRefusals(checks: RomBackupCheck[] | null | undefined): RomBackupCheck[] {
  const covered = new Set(Object.values(SERVER_CHECK));
  return (checks ?? []).filter((check) => !check.ok && !covered.has(check.id));
}

/** Why Start is disabled, or null when it may be pressed. `job` is the last
 *  `GET /control/rom/backup`: its `can_start`/`blocked_by` are the daemon's
 *  verdict over every check. */
export function backupStartBlocker(
  token: string,
  status: StatusResponse | null,
  job: Pick<RomBackupState, "state" | "preconditions" | "can_start" | "blocked_by"> | null,
): string | null {
  if (!token) return "Enter the control token to back up the ECU.";
  if (job?.state === "running") return "A backup is already running.";
  if (!status) return "Waiting for /status to confirm the preconditions.";
  const chip = backupPreconditions(status, job?.preconditions ?? null).find(
    (item) => item.state !== "ok",
  );
  if (chip) return chip.fix;
  if (job?.can_start === false) return job.blocked_by ?? "The daemon refuses to start a backup.";
  return null;
}

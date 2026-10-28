// ROM library, memory map, compare and ECU backup payloads (W08.3–W08.7).
//
// Every shape the ROM Library page reads is declared here and nowhere else,
// so the frontend's assumptions about the W08 routes can be reconciled with
// the daemon's in one file. Addresses are plain integers (byte offsets into
// the plain image), and every `end` is inclusive; the page formats them as
// hex. Field names follow `src/analysis/rom_image.py` and
// `src/control/rom_library.py` as drafted on 2026-10-04; fields marked
// "page reads" are the ones the UI depends on.

/** How an image entered the library. An image added twice keeps one entry
 *  and gains the second source. */
export type RomSource = "upload_plain" | "upload_osirisrom" | "ecu_read" | "rom_editor_export";

/** Where a ROM Editor export came from (`rom_editor.export_edits`): the parent
 *  image, the edit spec, the map and the calibration it was made with. */
export interface RomExportMeta {
  kind: "rom_editor_export";
  label: string;
  parent_sha256: string;
  /** The parent's identity: `{source: "revision", revision}` or a file. */
  parent: Record<string, unknown>;
  spec_sha256: string;
  spec_yaml: string;
  rom_map: { profile: string; file: string; sha256: string };
  calibration_id: string;
  checksums: {
    algorithm: string;
    parent: { sum: string; xor: string };
    written: { sum: string; xor: string };
  };
  cells: {
    table: string;
    slot: number | null;
    row: number;
    col: number;
    address: number;
    old: number;
    new: number;
  }[];
  image_sha256: string;
}

/** How sure the map definition is of an entry: checked against an independent
 *  known answer, strong evidence not yet confirmed, or bytes without a
 *  meaning. A field, never prose (W08.1). */
export type RomConfidence = "verified" | "inferred" | "unknown";

/** A 4 KiB block's statistical class. Describes what the bytes look like,
 *  not what they are. */
export type RomBlockClass = "erased" | "dense" | "data" | "sparse";

export type RomRegionKind = "boot" | "calibration" | "application" | "uprev" | "erased" | "unknown";

/** The vehicle checksum recomputed against the stored words. Shown whether it
 *  passes or fails; a failing image is reported, never refused. Words are
 *  keyed by name (`sum`, `xor`), as hex strings. */
export interface RomChecksumVerdict {
  ok: boolean;
  stored: Record<string, string | number> | null;
  computed: Record<string, string | number> | null;
  /** Which words differ, when it fails. */
  reason?: string | null;
  algorithm?: string;
}

/** Tune-registry match by plain-image sha256 (W08.2).
 *  `matched`: a flashed revision's `image_sha256` (`revision` set).
 *  `reference`: a known image never flashed as-is, such as a tuner's base ROM
 *  (`reference` set). `unrecognised`: neither. `unknown`: the flash history
 *  could not be read, so the image was never compared (`reason` says why). */
export interface RomRevisionMatch {
  revision: string | null;
  status: "matched" | "reference" | "unrecognised" | "unknown";
  reference?: string | null;
  reason?: string | null;
}

/** Read metadata for an `ecu_read` image (W08.7). An image that failed its
 *  integrity checks is kept with `verified: false` and the reason. */
export interface RomReadMeta {
  started_at?: string | null;
  ended_at?: string | null;
  duration_s?: number | null;
  requests?: number | null;
  retries?: number | null;
  verified?: boolean | null;
  unverified_reason?: string | null;
  [key: string]: unknown;
}

/** One time an image was added: the per-source provenance record. */
export interface RomSourceRecord {
  source: RomSource;
  filename?: string | null;
  added_at: string;
  container_sha256?: string | null;
  read_meta?: RomReadMeta | null;
  export_meta?: RomExportMeta | null;
}

/** What an image's own vector tables say about its size (`rom_extent`).
 *  `larger`: a valid table points past the image's end, below RAM, so the
 *  flash is bigger than the read. `no_sign`: nothing points past it, which is
 *  a sign, not proof. `unknown`: no table could be read. Call targets are
 *  evidence only, never the verdict. */
export type RomExtentVerdict = "larger" | "no_sign" | "unknown";

export interface RomExtentTable {
  offset: string;
  entries: number;
  valid: boolean;
  reason: string | null;
  inside: number;
  ram: number;
  erased: number;
  beyond: string[];
  beyond_count: number;
}

export interface RomExtentReading {
  verdict: RomExtentVerdict;
  /** One plain sentence naming what was seen, and where. */
  summary: string;
  image_size: number;
  ram_from: string;
  tables: RomExtentTable[];
  /** Null when the vehicle's recipe counts no call targets. */
  calls: {
    pairs: number;
    inside: number;
    ram: number;
    beyond: string[];
    beyond_count: number;
    beyond_pairs: number;
  } | null;
}

/** One stored image: `GET /control/rom/library` entries, the
 *  `POST /control/rom/library` response, and the `PATCH` response. */
export interface RomLibraryEntry {
  /** sha256 of the plain image; the library's content address. */
  sha256: string;
  crc32: string;
  /** sha256 of the `.OsirisROM` container, for an Osiris upload. */
  container_sha256: string | null;
  /** The first source the image arrived by. */
  source: RomSource | null;
  /** Every source the image arrived by, deduplicated, oldest first. */
  sources?: RomSource[];
  source_records?: RomSourceRecord[];
  /** The first upload filename; null for an image only ever read from the ECU. */
  filename: string | null;
  label: string | null;
  /** ISO 8601: first added. */
  added_at: string;
  /** ISO 8601: most recently added (the list's sort key, newest first). */
  last_added_at?: string;
  /** Read at the map definition's declared offset; null when unreadable. */
  calibration_id: string | null;
  /** Null when there is no map definition to compute it from. */
  checksum: RomChecksumVerdict | null;
  revision_match: RomRevisionMatch;
  /** Why the vehicle's map does not apply to this image, when it does not. */
  map_reason?: string | null;
  /** Null when the vehicle's backup recipe names no vector tables to read. */
  extent?: RomExtentReading | null;
  size: number;
  /** The latest ECU read's metadata; null when never read from the ECU. */
  read_meta: RomReadMeta | null;
  /** The latest ROM Editor export that made these bytes; null otherwise. */
  export_meta?: RomExportMeta | null;
}

/** `POST /control/rom/library`: the entry, and whether it is new. An image
 *  already in the library keeps its entry and gains the source. */
export type RomUploadResponse = RomLibraryEntry & { created?: boolean };

/** `GET /control/rom/library` — newest first. */
export interface RomLibraryResponse {
  entries: RomLibraryEntry[];
  count?: number;
}

/** One 4 KiB block's statistics (W08.2). */
export interface RomBlock {
  /** Byte offset of the block's first byte. */
  offset: number;
  /** Percentage (0–100) of the block's bytes that are 0xFF. */
  ff_pct: number;
  /** Shannon entropy, bits per byte (0–8). */
  entropy: number;
  class: RomBlockClass;
}

/** One region of the vehicle's map definition. Absolute regions have
 *  `slot: null`; each map slot's spans are resolved per slot, at absolute
 *  addresses, named `Map <n>: <name>`, with `slot` set. */
export interface RomRegion {
  start: number;
  /** Inclusive: the region's last byte. */
  end: number;
  size?: number;
  name: string;
  kind: RomRegionKind;
  confidence: RomConfidence;
  added_by_uprev: boolean;
  evidence: string;
  slot?: number | null;
}

/** A table placement as the description carries it (no values). The page
 *  reads table values from `/tables`; this list is not rendered. */
export interface RomTableDef {
  id?: string;
  kind?: string | null;
  name: string;
  label?: string;
  slot?: number | null;
  offset?: number | null;
  end?: number;
  rows?: number;
  cols?: number;
  dtype?: "u8" | "be16";
  role?: "axis" | "table";
  /** The declared decode, or null when the table is shown raw. */
  decode?: RomDecode | null;
  /** Axis table ids (the `/tables` payload resolves them to values). */
  axes?: { rows: string | null; cols: string | null };
  confidence: RomConfidence;
  evidence: string;
}

/** UpRev-style map slots: `count` slots of `stride` bytes from `base`;
 *  slot numbers are 1-based (W08.1 `MapSlots`). Their inner spans arrive in
 *  `RomDescription.regions` with `slot` set. */
export interface RomMapSlots {
  base: number;
  stride: number;
  count: number;
  end?: number;
  confidence?: RomConfidence;
  evidence?: string;
  slots?: { slot: number; start: number; end: number }[];
}

/** One erase sector: the unit the flash erases (W08.1 `EraseSector`). */
export interface RomEraseSector {
  index: number;
  start: number;
  /** Inclusive. */
  end: number;
  size?: number;
  confidence?: RomConfidence;
  evidence?: string;
}

/** The map definition that matched the image's calibration ID. */
export interface RomMapInfo {
  definition?: string;
  calibration_id?: string | null;
  calibration_ids?: string[];
  image_size?: number;
  /** Page reads: summarised above the region table. */
  erase_sectors?: RomEraseSector[];
  [key: string]: unknown;
}

/** `GET /control/rom/library/{sha}` — the full W08.2 description. */
export interface RomDescription {
  sha256: string;
  crc32: string;
  container_sha256: string | null;
  calibration_id: string | null;
  size: number;
  checksum: RomChecksumVerdict | null;
  revision_match: RomRevisionMatch;
  blocks: RomBlock[];
  /** Empty when `map` is null. */
  regions: RomRegion[];
  tables: RomTableDef[];
  map_slots: RomMapSlots | null;
  /** Null when no map definition covers this calibration. */
  map: RomMapInfo | null;
  /** Why `map` is null: "no map definition for calibration <id>". */
  map_reason?: string | null;
  /** The library entry, as the list carries it. */
  entry?: RomLibraryEntry;
}

/** A declared decode (W08.1 `Decode`), e.g. `afr = 1881.6 / raw`. */
export interface RomDecode {
  quantity: string;
  unit: string;
  formula?: string;
  kind?: "linear" | "reciprocal";
  confidence?: "verified" | "inferred";
  evidence?: string;
}

/** A table axis with its values resolved from the axis table it references. */
export interface RomTableAxis {
  id?: string;
  name: string;
  offset?: number;
  raw: number[];
  /** Present when the axis declares a decode (e.g. `rpm = raw × 50`); a
   *  reciprocal decode of a raw 0 has no value. */
  decoded?: (number | null)[] | null;
  unit?: string | null;
  decode?: RomDecode | null;
  confidence?: RomConfidence;
  /** How a tuning editor labels these breakpoints (e.g. UpRev's BFS in ms),
   *  computed from this image; the decode stays the code's own units. */
  editor_labels?: RomEditorLabels | null;
}

/** `EditorLabels` in src/analysis/rom_map.py: an editor's labels for an axis
 *  whose code compares raw counts, relative to another table's value. */
export interface RomEditorLabels {
  /** The tool that shows these labels, e.g. `UpRev`. */
  editor: string;
  quantity: string;
  unit: string;
  formula: string;
  /** Null when the table they are relative to has no value in this image. */
  values: number[] | null;
  confidence: "verified" | "inferred";
  evidence: string;
}

/** One table in one slot: `GET /control/rom/library/{sha}/tables` (W08.4).
 *  A table whose decode is unconfirmed has no `decoded` and is shown raw. */
/** Where a tuning editor labels a 2-D table's axes differently from the
 *  ECM's code (`EditorDisagreement` in src/analysis/rom_map.py). Data, so
 *  every view of the table warns from the same record. */
export interface RomEditorDisagreement {
  /** The tool whose display disagrees, e.g. `UpRev`. */
  editor: string;
  /** What the editor labels the rows with. */
  editor_row_labels: string;
  /** What the editor labels the columns with. */
  editor_col_labels: string;
  /** Row i, column j in the editor is the same memory cell as here. */
  positions_match: boolean;
  consequence: string;
  /** How to recover the true breakpoints from the editor's labels. */
  translation: string;
  evidence: string;
}

/** Where the ROM Editor's table tree files a table (`rom_map.py`
 *  `TableGroup`, W10.7). */
export type RomTableGroup = "fuel" | "ignition" | "airflow" | "emissions";

export interface RomTableValues {
  /** The definition's table id, unique per file. */
  id?: string;
  name: string;
  /** `Fuel Targets (Map 1)`. */
  label?: string;
  /** The tree's label when `name` is too long for it (W10.7). */
  short_name?: string | null;
  /** The tree's group; null on an axis. Absent from a daemon older than
   *  W10.7, whose tables the tree files under Other. */
  group?: RomTableGroup | null;
  /** Map slot (1-based) for a per-slot table; null for a single-copy table. */
  slot: number | null;
  /** Stable kind shared by every copy of one table (`fuel_targets`). Page
   *  reads: `fuel_targets` is drawn with `MapTable`. */
  kind?: string | null;
  /** `axis` for a table another table uses as an axis. */
  role?: "axis" | "table";
  /** `rom_map.yaml` marks it editable (never an axis). Whether the page
   *  takes an edit to this copy is `isEditableTable`'s: Map 1 or single-copy. */
  editable: boolean;
  decode?: RomDecode | null;
  /** Absolute byte offset of the table's first cell in this slot. */
  offset?: number | null;
  /** Inclusive last byte. */
  end?: number;
  rows?: number;
  cols?: number;
  dtype?: "u8" | "be16";
  unit?: string | null;
  /** Rows × cols; a scalar (K) is `[[value]]`. */
  raw: number[][];
  /** Null cells: a reciprocal decode of a raw 0. */
  decoded?: (number | null)[][] | null;
  /** `rows` labels each row, `cols` each column; either may be null. */
  axes: { rows?: RomTableAxis | null; cols?: RomTableAxis | null };
  confidence: RomConfidence;
  evidence: string;
  /** Page reads: shown as a warning on every slot copy of the table. */
  editor_disagreement?: RomEditorDisagreement | null;
}

export interface RomTablesResponse {
  sha256: string;
  calibration_id?: string | null;
  /** The slot numbers the map defines; empty without a map. */
  map_slots?: number[];
  tables: RomTableValues[];
  /** Why `tables` is empty (no map definition), when it is. */
  reason?: string | null;
}

/** One changed cell inside a table range of a compare. */
export interface RomCompareCell {
  /** The table's id. */
  table?: string | null;
  /** `Fuel Targets (Map 1)`. Page reads, before `table`. */
  table_label?: string | null;
  slot?: number | null;
  row: number;
  col: number;
  address?: number;
  old: number;
  new: number;
  old_decoded?: number | null;
  new_decoded?: number | null;
  unit?: string | null;
}

/** What a changed range overlaps: a table, a checksum word or a region. */
export interface RomCompareOverlap {
  type: "table" | "checksum" | "region" | "slot_region";
  id: string | null;
  label: string;
  slot: number | null;
  confidence: RomConfidence;
}

/** One changed byte range, merged with a 16-byte gap (W08.5). */
export interface RomCompareRange {
  start: number;
  /** Inclusive. */
  end: number;
  /** The range's span, unchanged bytes inside the merge gap included. */
  length: number;
  /** Bytes that differ inside the range. */
  changed?: number;
  /** The tables, checksum words and regions the range overlaps. */
  labels: string[];
  overlaps?: RomCompareOverlap[];
  cells?: RomCompareCell[];
}

/** `GET /control/rom/compare?a=<sha>&b=<sha>`. */
export interface RomCompareResponse {
  a: string;
  b: string;
  ranges: RomCompareRange[];
  /** Bytes that differ (not the merged ranges' span). */
  changed_bytes?: number;
  gap?: number;
  /** False when the map describes only one of the images: the ranges are
   *  then unlabelled, and `map_reason` says why. */
  map_applied?: boolean;
  map_reason?: string | null;
}

export type RomBackupStateName = "idle" | "refused" | "running" | "done" | "failed" | "cancelled";

/** What the finished read was checked against (W08.7). Null when the checks
 *  themselves failed (the reason says so). */
export interface RomBackupIntegrity {
  checksum: RomChecksumVerdict | null;
  /** `offset`: where the ID sits in the image (the map's offset, or where the
   *  ECM-reported ID was found when no map describes the image). */
  calibration_id: {
    image: string | null;
    ecu: string | null;
    ok: boolean;
    offset?: string | null;
  };
  reread: { blocks: number; mismatches: string[] };
  /** Null when the recipe has no `extent`. */
  extent?: RomExtentReading | null;
}

/** One start precondition as the daemon evaluates it. `id` is one of
 *  `backup`, `recipe`, `obd_source`, `vehicle_state`, `engine_watch`,
 *  `passive`, `foreign_tester`, `ecm_session`, `capture`. */
export interface RomBackupCheck {
  id: string;
  ok: boolean;
  /** The refusal when not ok; what was checked when ok. */
  detail: string;
}

/** `GET /control/rom/backup`, and the response of its POST routes (W08.7). */
export interface RomBackupState {
  state: RomBackupStateName;
  /** Why it was refused, failed or cancelled, or why a `done` read is
   *  unverified (a check with nothing to check against); names its cause. */
  reason?: string | null;
  /** What a running (or just-finished) read is doing. */
  phase?: string | null;
  blocks_done: number;
  blocks_total: number;
  bytes: number;
  bytes_total?: number;
  elapsed_s: number;
  /** Null until the rate is known. */
  eta_s: number | null;
  retries: number;
  requests?: number;
  timeouts?: number;
  /** CAN send errors (the bus refused a frame), retried like timeouts. */
  send_errors?: number;
  /** Negative response code → count. */
  negative_responses?: Record<string, number>;
  started_at?: string | null;
  ended_at?: string | null;
  /** True once the read image is in the library. A read that finished but
   *  could not be stored is `failed` with `stored: false` and no `sha256`. */
  stored?: boolean;
  /** Library sha256 of the image: set only when `stored` (on `done`, and on a
   *  `failed` read whose image was kept as unverified). */
  sha256?: string | null;
  /** False: kept, marked unverified. On `failed` a check disagreed (checksum,
   *  calibration ID or re-read); on `done` one could not run (no ROM map yet,
   *  or no ECM-reported calibration ID). */
  verified?: boolean | null;
  integrity?: RomBackupIntegrity | null;
  revision_match?: RomRevisionMatch | null;
  /** Seconds from the read's end until normal polling was seen again. */
  polling_resumed_s?: number | null;
  polling_resume_note?: string | null;
  /** The daemon's own start checks, all evaluated; null while running. */
  preconditions?: RomBackupCheck[] | null;
  can_start?: boolean;
  /** Every failing check's detail, joined; null when Start may be pressed. */
  blocked_by?: string | null;
}

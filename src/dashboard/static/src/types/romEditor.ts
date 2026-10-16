// ROM Editor payloads (W09.3). Every shape the editor page and
// `lib/romEditor.ts` read from or send to the edit-preview route is declared
// here and nowhere else, so the frontend's reading of the route can be
// reconciled with the daemon's in one file. Field names follow
// `src/analysis/rom_edit_spec.py` (`Edit`) and `src/analysis/rom_editor.py`
// (`preview_edits`) as of 2026-10-15.

import type { RomDecode, RomTableValues } from "./romLibrary";

/** The declared decode as `GET …/tables` sends it, plus the numeric
 *  parameters when the route sends them. As of 2026-10-15 the route sends
 *  only `formula` / `kind` / `quantity` / `unit`, so `lib/romEditor` reads
 *  the parameters off the formula when they are absent. */
export type RomEditorDecode = RomDecode & {
  scale?: number | null;
  offset?: number | null;
  numerator?: number | null;
  mask?: number | null;
};

/** `rom_edit_spec.Rounding`: a scale has no default (`rounding_undeclared`). */
export type RomRounding = "nearest" | "floor" | "ceil";

/** A `[row, col]` cell index into a table's `raw` grid. */
export type CellIndex = [number, number];

/** `rom_edit_spec.Region`: only the editor's server-side YAML round trip
 *  uses it; the page itself selects cells. */
export interface RomEditRegion {
  rpm?: [number, number];
  load?: [number, number];
  units: Partial<Record<"rpm" | "load", string>>;
}

/** `rom_edit_spec.Edit`: one table, one selector, one operation. The page
 *  builds `cells` edits only, and exactly one of `set_raw` / `add_raw` /
 *  `scale`; `rounding` goes with `scale` and with nothing else. */
export interface RomEdit {
  /** A `rom_map.yaml` table id (`fuel_targets`, `fuel_compensation`, `k_constant`). */
  table: string;
  slot?: number | null;
  cells?: CellIndex[];
  all?: true;
  region?: RomEditRegion;
  set_raw?: number;
  add_raw?: number;
  scale?: number;
  rounding?: RomRounding;
}

/** `POST /control/rom/library/{sha}/edits/preview` body. */
export interface RomEditPreviewRequest {
  label: string;
  edits: RomEdit[];
}

/** One changed cell, old -> new raw (`rom_edit_spec._cell_record`). */
export interface RomEditedCell {
  table: string;
  slot: number | null;
  row: number;
  col: number;
  address: number;
  old: number;
  new: number;
}

/** What the change reaches (`provenance.effect`). */
export interface RomEditEffect {
  outcome: string;
  slot: number | null;
  slot_source: string;
  changed_bytes: number;
  /** Application-model nodes the changed bytes feed. */
  changed_nodes: string[];
  /** Lookups read at a moved operating point (e.g. Target AFR after BFS moves). */
  moved_lookups: Record<string, unknown>[];
  rom_effects?: Record<string, unknown>;
}

/** `rom_edit_spec._provenance`, the fields the page reads. */
export interface RomEditProvenance {
  kind: string;
  flashable: boolean;
  label: string;
  base_sha256: string;
  candidate_sha256: string;
  spec_sha256: string | null;
  cells: RomEditedCell[];
  ranges: Record<string, unknown>[];
  effect: RomEditEffect;
}

/** Whether the base can be simulated (`rom_editor.sim_base`). `ok` is true
 *  only for a library entry whose revision is recorded as `matched`. */
export interface RomSimBase {
  ok: boolean;
  status?: string | null;
  revision: string | null;
  /** `base_not_a_recorded_revision` when not ok. */
  reason: string | null;
}

/** A 200 from the preview route (`rom_editor.preview_edits`). The candidate
 *  image is never part of it, by design: only its sha256. */
export interface RomEditPreview {
  tool: string;
  tool_version: string;
  /** The `EditSpec` as JSON. */
  spec: Record<string, unknown>;
  /** The spec as YAML: what "Download spec" saves. */
  spec_yaml: string;
  spec_sha256: string;
  candidate_sha256: string;
  provenance: RomEditProvenance;
  /** The edited tables after the edits, in the `GET …/tables` shape. */
  tables: RomTableValues[];
  sim_base: RomSimBase;
}

/** A 400 from the preview route: `{error: "edit_refused", reason, message}`. */
export interface RomEditRefusal {
  reason: string;
  message: string;
}

/** A cell selection: a rectangle from `anchor` to `focus` (both inclusive,
 *  either may be the larger corner). */
export interface CellSelection {
  anchor: CellIndex;
  focus: CellIndex;
}

/** The inclusive bounds of a selection, normalized. */
export interface SelectionRange {
  r0: number;
  r1: number;
  c0: number;
  c1: number;
}

/** An operation the operator applies to a selection. */
export type RomEditOperation =
  /** An engineering value (AFR 12.5, 103 %), inverted to a raw by the decode. */
  | { kind: "set_value"; value: number }
  | { kind: "set_raw"; raw: number }
  | { kind: "add_raw"; delta: number }
  /** `percent` 2 means x 1.02. `rounding` is null until the operator picks
   *  one: there is no default. */
  | { kind: "scale"; percent: number; rounding: RomRounding | null };

/** One selected cell's change as the browser computes it, before the server
 *  does: raw old -> new, with the decoded values where a decode exists. */
export interface PlannedCell {
  row: number;
  col: number;
  oldRaw: number;
  newRaw: number;
  oldDecoded: number | null;
  newDecoded: number | null;
}

/** An applied operation: the edit to send and what it will do, as shown in
 *  the pending list. Persisted per base image. */
export interface PendingEdit {
  /** Stable key for the list. */
  id: string;
  /** Sentence for the list, e.g. `scale x1.02 (nearest) on 6 cells`. */
  summary: string;
  edit: RomEdit;
  /** Table name as the operator reads it (`Fuel Targets`). */
  tableName: string;
  /** The `12.5 AFR → raw 151 → 12.46 AFR` chain of a set-to-value, else null. */
  chain: string | null;
}

/** buildEdits' answer: edits to send, or a refusal made in the browser. */
export type BuildResult =
  | { ok: true; edits: RomEdit[]; cells: PlannedCell[]; chain: string | null }
  | { ok: false; reason: string; message: string };

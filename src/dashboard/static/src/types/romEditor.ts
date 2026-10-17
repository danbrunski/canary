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

/** One reason a number is withheld (`rom_levers.Blocker`). `text` is what
 *  the classifier's `messages` carry; the page shows `messages`, not these. */
export interface RomLeverBlocker {
  code: string;
  text: string;
  node: string | null;
  detail: string | null;
}

/** One lever the edit pulls, with the bytes and tables behind it
 *  (`rom_levers.LeverEntry`). */
export interface RomLeverEntry {
  /** The rom map's table `kind` (`fuel_compensation`, `timing_trim_a`, …),
   *  or `unmapped` / `checksum` / `constant`. */
  lever: string;
  label: string;
  tables: string[];
  slots: number[];
  byte_class: string;
  start: number;
  end: number;
  changed_bytes: number;
  /** Cells the edit resolved to; null for an image saved elsewhere. */
  changed_cells: number | null;
  nodes: string[];
  blockers: RomLeverBlocker[];
}

/** One output: whether the edit reaches it and what blocks a number
 *  (`rom_levers.OutputBlockers`, with the three values `to_dict` adds). */
export interface RomLeverOutput {
  output: string;
  source: "application_model" | "simulated";
  status: string;
  levers: string[];
  blockers: RomLeverBlocker[];
  assumes_conditioned: string[];
  refused_if_conditioned: string[];
  reached: boolean;
  /** A number can be given: reached, and nothing blocks it. */
  number: boolean;
  design_dependent: boolean;
}

/** `rom_levers.LeverClassification.to_dict()`: which lever an edit pulls and,
 *  per output, what stops a number. `messages` is the text `tools/rom_sim.py
 *  inspect` prints for the same edit, one fact per line, in a stable order.
 *  The classifier's floor is one line ("the edit reaches no output"), so an
 *  empty `messages` is not something it sends: it is never an all-clear. */
export interface RomLevers {
  schema_version: 1;
  tool: string;
  tool_version: string;
  /** The comparison's outcome: `identical`, `evaluable`, `partial`, `unevaluable`. */
  outcome: string;
  /** For an image the comparison could not use: the refusal. */
  reason: string | null;
  regime: string | null;
  levers: RomLeverEntry[];
  outputs: RomLeverOutput[];
  messages: string[];
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
  /** Which lever the edit pulls and what blocks a number (E09.14b). */
  levers: RomLevers;
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

// -- Simulate (W09.4) ---------------------------------------------------------------------
// `POST /control/rom/library/{sha}/simulate`. The request is
// `rom_editor.SimRequest` (21 fields, none defaulted, `extra="forbid"`); the
// response is `rom_editor.simulate_edits`, whose `cells` is
// `sim_cells.CellTable.to_dict()` (E08.4, `schema_version` 1). Field names
// follow `docs/headless-api.md` "ROM Editor: preview and simulate (W09)".

/** `sim_cells.Grid`: which table's axes the cells are aggregated on. */
export type SimGrid = "fuel_comp" | "target_afr";

/** `sim_design.LoadBasis`: today's only value. */
export type SimLoadBasis = "fill_fraction_as_manifold_ratio";

/** `none`: both ROMs start from the same learned state; `ecu_reflash`: the
 *  flash resets what the contract says it resets on the candidate. */
export type SimCandidateEvents = "none" | "ecu_reflash";

/** A stated rail pressure, or the plant's own working value, said so. */
export type SimRail = number | "working_assumption";

/** The body of the simulate route. Every field is required. */
export interface RomSimRequest {
  label: string;
  edits: RomEdit[];
  grid: SimGrid;
  rpm_edges: number[];
  /** In Fuel Comp's load axis units (% of the fill model's ceiling), 0-100. */
  load_edges: number[];
  load_basis: SimLoadBasis;
  baro_kpa: number;
  iat_c: number;
  coolant_c: number;
  battery_v: number;
  rail_kpa: SimRail;
  throttle_pct: number;
  /** An integer 0-255. */
  pedal_flag: number;
  stft_pct: number;
  ltft_pct: number;
  throttle_closed_learned: number;
  idle_air_learned: number;
  evap_purge_area: number;
  candidate_events: SimCandidateEvents;
  /** An integer >= 1, at most `MAX_DRAWS`. */
  draws: number;
  /** An integer >= 0. */
  seed: number;
}

/** What a declared value is (`rom_editor.TYPICAL_KINDS`). */
export type RomTypicalKind = "typical" | "placeholder";

/** `GET /control/rom/editor/typical-values` (W09.11): the vehicle's declared
 *  typical value for one request field. `source` is required server-side: an
 *  unsourced value cannot be loaded, so none reaches the page. */
export interface RomTypicalValue {
  value: SimRail;
  /** `typical`: a sourced figure for the vehicle. `placeholder`: a number the
   *  simulator reads nowhere, filled only so the form can run; the page tags it
   *  `placeholder`, never `typical`. */
  kind: RomTypicalKind;
  source: string;
}

/** A fillable field the vehicle declares no typical value for, and why. */
export interface RomTypicalLeftEmpty {
  reason: string;
}

/** The typical-values route's body. Keyed by `RomSimRequest` field name; the
 *  keys are strings, not `keyof RomSimRequest`, because a newer daemon may
 *  name a field this dashboard cannot fill (the page says so). The twelve it
 *  can fill are `lib/romSimTypical.ts` `TYPICAL_TARGETS`. */
export interface RomTypicalValues {
  profile: string;
  file: string;
  file_sha256: string;
  values: Record<string, RomTypicalValue>;
  left_empty: Record<string, RomTypicalLeftEmpty>;
}

/** `sim_cells.STATUSES`: the status of one output in one cell. */
export type SimStatus =
  "changed" | "unchanged" | "unsupported" | "partly_supported" | "no_points" | "not_reached";

export interface SimQuantiles {
  p05: number;
  p50: number;
  p95: number;
}

/** Weighted quantiles over the pairs `over` names. */
export interface SimQuantileBlock {
  over: "all_pairs" | "supported_pairs" | "computed_unsupported_pairs";
  pairs: number;
  weight: number;
  delta: SimQuantiles;
  /** Null for an output with no relative delta (the trim), or when no pair
   *  has a nonzero base. */
  relative_delta: SimQuantiles | null;
  relative_undefined_pairs: number;
}

export interface SimSupportEntry {
  status: "supported" | "unsupported";
  reasons: string[];
  pairs: number;
  weight_share: number;
}

/** One output in one cell (or one loop kind of a cell). */
export interface SimOutputCell {
  status: SimStatus;
  pairs: number;
  supported_share: number | null;
  unsupported_share: number | null;
  support: SimSupportEntry[];
  quantiles: SimQuantileBlock | null;
  /** Share of the supported weight whose delta is not exactly 0. */
  changed_share: number | null;
  /** `partly_supported`: what the supported pairs show. */
  supported_effect?: "changed" | "unchanged" | null;
}

/** One loop kind's pairs in a cell, and its headline output. */
export interface SimLoopBlock {
  loop: string;
  weight_share: number;
  pairs: number;
  output: string;
  cell: SimOutputCell;
}

export interface SimCell {
  row: number;
  col: number;
  row_breakpoint: number;
  col_breakpoint: number;
  row_value: number | null;
  col_value: number | null;
  n_points: number;
  /** Design point indices; the page does not render them. */
  points?: number[];
  pairs: number;
  weight: number;
  /** Shares of the cell's weight by the design's regime label. */
  regime_shares: Record<string, number>;
  /** Shares by the simulated loop (`open_loop`, `closed_loop`, ...). */
  loop_shares: Record<string, number>;
  outputs: Record<string, SimOutputCell>;
  by_loop: SimLoopBlock[];
  /** This table cell is edited (raw old -> new). */
  edited: { old: number; new: number } | null;
  row_axis_moved: boolean;
  col_axis_moved: boolean;
}

export interface SimAxisInfo {
  table: string;
  coordinate: string;
  raw: number[];
  breakpoints: number[];
  values: (number | null)[];
  unit: string | null;
  domain: string;
  /** Whole coordinate values each index holds, inclusive (null: unbounded). */
  cell_ranges: [number | null, number | null][];
}

/** Whether the change can reach an output. */
export interface SimReach {
  status: "reached" | "not_reached" | "no_change";
  basis: string[];
}

export interface SimOverlay {
  edited_cells: { row: number; col: number; old: number; new: number }[];
  axis_edits: { axis: "rows" | "cols"; table: string; index: number; old: number; new: number }[];
  other_edits: { table: string; slot: number | null; cells: number }[];
  moved_lookups: { node: string; tables: string[]; on_this_grid: boolean }[];
}

/** `sim_cells.CellTable.to_dict()`. Every cell of the grid is present,
 *  row-major, visited or not. */
export interface SimCellTable {
  schema_version: 1;
  tool: string;
  tool_version: string;
  grid: SimGrid;
  table: string;
  slot: number | null;
  membership: string;
  membership_rule: string;
  quantiles: number[];
  quantile_method: string;
  weight_basis: string;
  base_image_sha256: string;
  candidate_image_sha256: string;
  design_sha256: string;
  design_kind: string;
  result_digest: string;
  draws: number;
  n_points: number;
  outputs: string[];
  /** Loop kind -> the output that is its headline. */
  headline_by_loop: Record<string, string>;
  rows: SimAxisInfo;
  cols: SimAxisInfo;
  reach: Record<string, SimReach>;
  caveats: string[];
  unresolved: string[];
  rom_outcome: string;
  weight_total: number;
  unplaced: Record<string, { pairs: number; weight: number }>;
  /** Output -> status -> number of cells. */
  status_counts: Record<string, Record<string, number>>;
  overlay: SimOverlay;
  cells: SimCell[];
}

/** `sim_design.summary(design)` plus its grid. The design's own assumptions are in
 *  the result's `assumptions`, "design: "-prefixed (E08.10), not here. */
export interface SimDesignSummary {
  sha256: string;
  kind: string;
  base_revision: string;
  points: number;
  weight: number;
  weight_basis: string;
  eligible_dwell_s?: number;
  reads?: number;
  plant_states?: Record<string, number>;
  placement?: Record<string, number>;
  unplaced_dwell_s?: Record<string, number>;
  weight_by_regime?: Record<string, number>;
  points_by_throttle_source?: Record<string, number>;
  points_by_battery_source?: Record<string, number>;
  battery_unmeasured_points?: number;
  battery_v_when_unmeasured?: number | null;
  fill_byte_width?: { max: number; median: number } | null;
  grid?: {
    rpm_edges: number[];
    load_edges: number[];
    load_axis: string;
    load_unit: string;
    load_basis: string;
    environment: Record<string, number | string>;
  };
}

/** The result's provenance: hashes, plant, seed and draws. Free-form beyond
 *  the fields the page names. */
export interface SimProvenance {
  base_image_sha256?: string;
  candidate_image_sha256?: string;
  candidate_events?: string[];
  design_sha256?: string;
  uncertainty_sha256?: string;
  plant_version?: string;
  baseline_sha256?: string | null;
  contract_sha256?: string;
  application_model_sha256?: string;
  seed?: number;
  draws?: number;
  rng?: string;
  [key: string]: unknown;
}

/** A 200 from the simulate route (`rom_editor.simulate_edits`). `label` here
 *  is the result's; the request's is `candidate.label`. */
export interface RomSimResult {
  tool: string;
  tool_version: string;
  label: string;
  synthetic: boolean;
  /** The result's caveats, first and unchanged. */
  caveats: string[];
  /** Plant working assumptions ("plant working assumption: ..."), the design's
   *  ("design: ...") and others; `caveats` repeats every one of them. */
  assumptions: string[];
  unresolved: string[];
  inert_parameters: string[];
  coverage: Record<string, number>;
  outcome: string;
  learned_state?: Record<string, unknown>;
  provenance: SimProvenance;
  design: SimDesignSummary;
  /** The preview's provenance: what the edits changed in the candidate. */
  candidate: RomEditProvenance;
  cells: SimCellTable;
  spec_yaml: string | null;
  spec_sha256?: string;
}

/** Why a simulate did not return a result, by the route's own error name. */
export type SimFailureKind =
  | "refused"
  | "deferred"
  | "busy"
  | "unavailable"
  | "invalid"
  | "not_found"
  | "unauthorized"
  | "error";

export interface SimFailure {
  kind: SimFailureKind;
  /** The route's `reason` (`capture_active`, `grid_too_large`, ...), or null. */
  reason: string | null;
  message: string;
  /** `sim_refused` only: `edit_spec`, `rom_effect`, `sim_design` or `simulation`. */
  refusalKind?: string | null;
  /** `analysis_deferred` only: the route's pointer to the laptop path. */
  laptopPath?: string | null;
  profile?: string | null;
}

/** What the page remembers about a run, next to its result: the request as
 *  sent, the edits as the operator read them, and the base. The result's own
 *  caveat block prints these, so a result never loses what it was run on. */
export interface SimRunContext {
  request: RomSimRequest;
  edits: { tableName: string; summary: string; chain: string | null }[];
  base: { sha256: string; label: string; revision: string | null };
  /** An edit wrote the last Fuel Targets column (the one the pedal flag's
   *  bit 0 forces); with that bit clear the run cannot show its effect. */
  wotColumnEdited: boolean;
  /** Wall-clock seconds the request took, as the page measured them. */
  elapsedS: number;
  /** The Fine preset's size for this base (W09.9): what a coarse run is
   *  judged against. Absent: only the draw count is judged. */
  fineReference?: { points: number | null; draws: number };
}

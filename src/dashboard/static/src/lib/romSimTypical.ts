// "Fill typical values" on the Simulate form (W09.11): what the vehicle's
// declared typical values (`data/<vehicle>/sim_typical_values.yaml`, served by
// `GET /control/rom/editor/typical-values`) put into the form, and how the page
// says so.
//
// What this makes impossible rather than documented:
// - nothing here is a vehicle fact: the values and their sources come from the
//   daemon's profile data. This file knows only which form fields exist, and
//   `TYPICAL_TARGETS` is checked against the shipped file in
//   `romSimTypical.test.ts`, so a field added on one side and not the other
//   fails a test;
// - nothing is filled unseen or unsourced: a value with no source, a value the
//   form's own checks refuse, or a field this dashboard has no input for is
//   *skipped and named*, never filled;
// - the click fills exactly the fields the vehicle declared. It never touches
//   the grid, the candidate events, the edges, the draws, the seed or the
//   label (none is in `TYPICAL_TARGETS`), and it says what it replaced of the
//   operator's own values;
// - a failed fetch fills nothing, and the reason is shown by name.

import { HttpError } from "../api/http";
import { describeError } from "../hooks/useControlResource";
import type { RomSimRequest, RomTypicalKind, RomTypicalValues } from "../types/romEditor";
import {
  ENVIRONMENT_FIELDS,
  FAILURE_TITLE,
  LEARNED_FIELDS,
  RAIL_FIELD,
  parseNumber,
  simFailure,
  type FormKey,
  type NumberFieldSpec,
  type SimFormValues,
} from "./romSim";

/** One request field the button can fill, and the form fields that hold it. */
export interface TypicalTarget {
  field: keyof RomSimRequest;
  /** As the form labels it: `Baro (kPa)`. */
  label: string;
  /** The form's own fields (rail pressure is a choice and a number). */
  keys: FormKey[];
  spec: NumberFieldSpec;
}

const nameOf = (spec: NumberFieldSpec) => (spec.unit ? `${spec.label} (${spec.unit})` : spec.label);

/** The twelve fields of the request that describe the vehicle's state: the
 *  stated environment and the learned state. Anything else the request holds
 *  (the grid, the edges, the candidate events, the draws, the seed, the label,
 *  the edits) describes this experiment or this run and is never filled. */
export const TYPICAL_TARGETS: readonly TypicalTarget[] = [
  ...ENVIRONMENT_FIELDS,
  RAIL_FIELD,
  ...LEARNED_FIELDS,
].map((spec) => ({
  field: spec.request,
  label: nameOf(spec),
  keys: spec.request === "rail_kpa" ? ["rail", "railKpa"] : [spec.key],
  spec,
}));

export const WORKING_ASSUMPTION_TEXT = "the plant's working assumption";

/** What a filled field's tag says: its kind and its source, from the vehicle's data. */
export interface TypicalMark {
  kind: RomTypicalKind;
  source: string;
}

/** The tag next to a filled field: `typical: <source>`, or `placeholder: <source>`
 *  for a number the simulator reads nowhere, so it cannot pass for a measurement. */
export const typicalTag = (mark: TypicalMark) => `${mark.kind}: ${mark.source}`;

export interface TypicalFilled {
  field: keyof RomSimRequest;
  label: string;
  /** What the field now shows. */
  shown: string;
  kind: RomTypicalKind;
  /** What the operator had stated there, when it differed; else null. */
  replaced: string | null;
  source: string;
  keys: FormKey[];
}

export interface TypicalLeft {
  field: string;
  label: string;
  reason: string;
  /** The vehicle named the field and gave a reason (else the daemon sent
   *  neither a value nor a reason). */
  declared: boolean;
}

export interface TypicalSkipped {
  field: string;
  label: string;
  why: string;
}

export interface TypicalFill {
  profile: string;
  file: string;
  sha8: string;
  /** Form fields to set. */
  values: Partial<SimFormValues>;
  /** Form field -> what its tag shows. */
  marks: Partial<Record<FormKey, TypicalMark>>;
  filled: TypicalFilled[];
  left: TypicalLeft[];
  skipped: TypicalSkipped[];
  /** The pedal flag as filled, for its one-line explanation. */
  pedalFlag: number | null;
}

const NO_REASON = "the daemon declared neither a typical value nor a reason for this field";

function numberText(value: unknown): string | null {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : null;
}

/** What the form shows now for a target, as text ("" when empty). */
function currentText(current: SimFormValues, target: TypicalTarget): string {
  if (target.field !== "rail_kpa") return current[target.keys[0]].trim();
  if (current.rail === "working_assumption") return WORKING_ASSUMPTION_TEXT;
  return current.rail === "stated" ? current.railKpa.trim() : "";
}

const sameNumber = (a: string, b: string) =>
  a !== "" && b !== "" && Number.isFinite(Number(a)) && Number(a) === Number(b);

/** What pressing the button does to `current`, from the daemon's declared
 *  values: the form fields to set, their tags, and a report of what was
 *  filled, replaced, left empty and skipped. Pure: the page applies it. */
export function typicalFill(current: SimFormValues, response: RomTypicalValues): TypicalFill {
  const fill: TypicalFill = {
    profile: response.profile,
    file: response.file,
    sha8: response.file_sha256.slice(0, 8),
    values: {},
    marks: {},
    filled: [],
    left: [],
    skipped: [],
    pedalFlag: null,
  };
  const set = (key: FormKey, value: string) => {
    (fill.values as Record<FormKey, string>)[key] = value;
  };
  for (const target of TYPICAL_TARGETS) {
    const declared = response.values[target.field];
    if (declared === undefined) {
      const empty = response.left_empty[target.field];
      const reason = typeof empty?.reason === "string" ? empty.reason.trim() : "";
      fill.left.push({
        field: target.field,
        label: target.label,
        reason: reason || NO_REASON,
        declared: reason !== "",
      });
      continue;
    }
    const skip = (why: string) =>
      fill.skipped.push({ field: target.field, label: target.label, why });
    const source = typeof declared.source === "string" ? declared.source.trim() : "";
    if (source === "") {
      skip("the daemon sent no source for it, and a value with no source is not filled");
      continue;
    }
    const kind = declared.kind;
    if (kind !== "typical" && kind !== "placeholder") {
      skip("the daemon named no kind for it (typical or placeholder)");
      continue;
    }
    let shown: string;
    if (target.field === "rail_kpa" && declared.value === "working_assumption") {
      shown = WORKING_ASSUMPTION_TEXT;
    } else {
      const text = numberText(declared.value);
      if (text === null) {
        skip(`its value ${JSON.stringify(declared.value)} is not a number`);
        continue;
      }
      const parsed = parseNumber(text, target.spec);
      if (parsed.kind !== "ok") {
        skip(parsed.kind === "invalid" ? parsed.message : "its value is empty");
        continue;
      }
      shown = text;
    }
    const before = currentText(current, target);
    fill.filled.push({
      field: target.field,
      label: target.label,
      shown,
      kind,
      replaced: before !== "" && before !== shown && !sameNumber(before, shown) ? before : null,
      source,
      keys: target.keys,
    });
    if (target.field === "rail_kpa") {
      if (declared.value === "working_assumption") {
        set("rail", "working_assumption");
        set("railKpa", "");
        fill.marks.rail = { kind, source };
      } else {
        set("rail", "stated");
        set("railKpa", shown);
        fill.marks.rail = { kind, source };
        fill.marks.railKpa = { kind, source };
      }
    } else {
      const key = target.keys[0];
      set(key, shown);
      fill.marks[key] = { kind, source };
    }
    if (target.field === "pedal_flag") fill.pedalFlag = Number(shown);
  }
  const known = new Set<string>(TYPICAL_TARGETS.map((target) => target.field));
  for (const field of [...Object.keys(response.values), ...Object.keys(response.left_empty)]) {
    if (!known.has(field) && !fill.skipped.some((item) => item.field === field)) {
      fill.skipped.push({
        field,
        label: field,
        why: "this dashboard has no form field for it",
      });
    }
  }
  return fill;
}

/** The first line of the report after a click. */
export function typicalHeadline(fill: TypicalFill): string {
  const n = fill.filled.length;
  const placeholders = fill.filled.filter((item) => item.kind === "placeholder").length;
  const counted =
    placeholders === 0
      ? `${n} typical ${n === 1 ? "value" : "values"}`
      : `${n} ${n === 1 ? "value" : "values"} (${n - placeholders} typical, ${placeholders} ${placeholders === 1 ? "placeholder" : "placeholders"})`;
  return `Filled ${counted} from ${fill.profile} (${fill.file}, sha256 ${fill.sha8}). Each is marked with its kind and source and is yours to edit.`;
}

/** The report's line about placeholders, or null when none was filled. A
 *  placeholder is a number filled so the form can run, not a measurement; its
 *  source (the vehicle's data) says why that is safe and when to replace it. */
export function placeholderLine(fill: TypicalFill): string | null {
  const labels = fill.filled.filter((item) => item.kind === "placeholder").map((i) => i.label);
  if (labels.length === 0) return null;
  const are = labels.length === 1 ? "is a placeholder" : "are placeholders";
  return `${labels.length} of them ${are}, not ${labels.length === 1 ? "a measurement" : "measurements"}: ${labels.join(", ")}. Their source says why that is safe and when to replace them.`;
}

/** What the pedal flag the click filled does, in one line. */
export function pedalFlagLine(flag: number): string {
  return Number.isInteger(flag) && (flag & 1) === 0
    ? `Pedal flag ${flag}: bit 0 is clear, so no simulated point reads the last Target AFR column (the WOT column) and an edit to that column shows no change.`
    : `Pedal flag ${flag}: bit 0 is set, so every simulated point reads the last Target AFR column (the WOT column).`;
}

/** After a click, the fields it did not touch. */
export const NOT_TOUCHED =
  "Nothing else was touched: the grid, the candidate events, the edges, the draws, the seed and the label stay as they were.";

export interface TypicalFailure {
  title: string;
  message: string;
  reason: string | null;
}

/** FastAPI's `detail` from an error body; null when the body is not JSON. */
function detailOf(body: string): unknown {
  try {
    return (JSON.parse(body) as { detail?: unknown } | null)?.detail ?? null;
  } catch {
    return null;
  }
}

/** Why the values could not be had, by reason. The form is left unchanged. */
export function typicalFailure(err: unknown): TypicalFailure {
  if (err instanceof HttpError) {
    const detail = detailOf(err.body);
    if (typeof detail === "object" && detail !== null) {
      const d = detail as Record<string, unknown>;
      const message = typeof d.message === "string" ? d.message : describeError(err);
      if (d.error === "typical_values_undeclared") {
        return {
          title: "This vehicle declares no typical values",
          message,
          reason: "typical_values_undeclared",
        };
      }
      if (d.error === "typical_values_invalid") {
        return {
          title: "This vehicle's typical values file is invalid",
          message,
          reason: "typical_values_invalid",
        };
      }
    }
    if (err.status === 401 || err.status === 403) {
      return { title: FAILURE_TITLE.unauthorized, message: describeError(err), reason: null };
    }
    if (err.status === 404) {
      return {
        title: "This daemon does not serve typical values",
        message: `${describeError(err)} The daemon may be older than this dashboard.`,
        reason: null,
      };
    }
  }
  const failure = simFailure(err);
  return { title: FAILURE_TITLE[failure.kind], message: failure.message, reason: failure.reason };
}

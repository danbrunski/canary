// The Simulate form's state as one reducer (R28.5): the values, which of them
// came back from the browser's storage, which a preset or the typical-values
// button filled, and where the grid edges came from. `SimForm` kept these as
// five `useState` cells with a `setX(current => ...)` per event; the events
// below are the ways the form changes, so one that forgets to drop a mark when
// a field is edited (the remember rules) has nowhere to live.
//
// What this makes impossible rather than documented:
// - editing a field takes its "remembered" and its "typical" mark with it, in
//   the one action that edits a field;
// - a preset or a typical fill takes the remembered mark off exactly the
//   fields it overwrote, and nothing else;
// - the typical fill reads the form as the reducer holds it when the values
//   arrive, not as it was at the click.
//
// Pure: storage (`loadRemembered`, `saveRemembered`, `forgetRemembered`) stays
// with the caller; this takes what was loaded and says what changed.

import type { RomTypicalValues } from "../types/romEditor";
import {
  EMPTY_FORM,
  formatEdges,
  seedForm,
  withChoice,
  type BreakpointSuggestion,
  type FormKey,
  type Remembered,
  type SimFormValues,
} from "./romSim";
import type { PresetFill, SimPreset } from "./romSimSize";
import {
  typicalFill,
  type TypicalFailure,
  type TypicalFill,
  type TypicalMark,
} from "./romSimTypical";

/** The "Fill typical values" button's state: nothing asked, fetching, the
 *  report of the last fill, or why the values could not be had. */
export type TypicalState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "done"; fill: TypicalFill }
  | { kind: "failed"; failure: TypicalFailure };

export interface SimFormState {
  values: SimFormValues;
  /** Fields whose value came back from storage and the operator has not touched. */
  remembered: ReadonlySet<FormKey>;
  /** When the remembered values were saved; null once none remain to mark. */
  savedAt: string | null;
  /** The edges say where they came from until the operator types. */
  typedEdges: { rpm: boolean; load: boolean };
  /** The preset last clicked and what it filled. Not stored: a preset is a
   *  button press on this page, never a remembered default. */
  applied: PresetFill | null;
  /** The fields "Fill typical values" filled, with the kind and source each
   *  shows (gone once the operator edits that field). Not stored either. */
  typical: ReadonlyMap<FormKey, TypicalMark>;
  typicalState: TypicalState;
}

export type SimFormAction =
  | { type: "change"; key: FormKey; value: string }
  | { type: "preset"; preset: SimPreset; fill: PresetFill }
  | { type: "resetEdges"; rpmEdges: number[]; loadEdges: number[] }
  | { type: "clearRemembered" }
  | { type: "typicalLoading" }
  | { type: "typicalFailed"; failure: TypicalFailure }
  | { type: "typicalFilled"; response: RomTypicalValues };

/** The form as first shown: empty but for the suggested edges (derived from the
 *  base image, shown with their source) and the remembered values laid over the
 *  fields still empty. */
export function initSimForm(
  suggestion: BreakpointSuggestion,
  remembered: Remembered,
): SimFormState {
  const base: SimFormValues = {
    ...EMPTY_FORM,
    rpmEdges: suggestion.rpmEdges ? formatEdges(suggestion.rpmEdges) : "",
    loadEdges: suggestion.loadEdges ? formatEdges(suggestion.loadEdges) : "",
  };
  const seeded = seedForm(base, remembered);
  return {
    values: seeded.values,
    remembered: new Set(seeded.remembered),
    savedAt: seeded.remembered.length > 0 ? remembered.savedAt : null,
    typedEdges: { rpm: false, load: false },
    applied: null,
    typical: new Map(),
    typicalState: { kind: "idle" },
  };
}

function withoutKeys(
  remembered: ReadonlySet<FormKey>,
  keys: readonly FormKey[],
): ReadonlySet<FormKey> {
  if (!keys.some((key) => remembered.has(key))) return remembered;
  const next = new Set(remembered);
  for (const key of keys) next.delete(key);
  return next;
}

export function simFormReducer(state: SimFormState, action: SimFormAction): SimFormState {
  switch (action.type) {
    case "change": {
      const { key, value } = action;
      let typical = state.typical;
      if (typical.has(key)) {
        // An edited field is the operator's now: its typical tag goes.
        const next = new Map(typical);
        next.delete(key);
        typical = next;
      }
      return {
        ...state,
        values: withChoice(state.values, key, value),
        remembered: withoutKeys(state.remembered, [key]),
        typical,
        typedEdges:
          key === "rpmEdges"
            ? { ...state.typedEdges, rpm: true }
            : key === "loadEdges"
              ? { ...state.typedEdges, load: true }
              : state.typedEdges,
      };
    }
    case "preset":
      return {
        ...state,
        values: {
          ...state.values,
          rpmEdges: formatEdges(action.fill.rpmEdges),
          loadEdges: formatEdges(action.fill.loadEdges),
          draws: String(action.fill.draws),
        },
        remembered: withoutKeys(state.remembered, ["draws"]),
        typedEdges: { rpm: action.preset === "coarse", load: action.preset === "coarse" },
        applied: action.fill,
      };
    case "resetEdges":
      return {
        ...state,
        values: {
          ...state.values,
          rpmEdges: formatEdges(action.rpmEdges),
          loadEdges: formatEdges(action.loadEdges),
        },
        typedEdges: { rpm: false, load: false },
      };
    case "clearRemembered": {
      const values = { ...state.values };
      for (const key of state.remembered) (values as Record<FormKey, string>)[key] = "";
      return { ...state, values, remembered: new Set(), savedAt: null };
    }
    case "typicalLoading":
      return { ...state, typicalState: { kind: "loading" } };
    case "typicalFailed":
      // The form is left exactly as it was; only the reason is shown.
      return { ...state, typicalState: { kind: "failed", failure: action.failure } };
    case "typicalFilled": {
      // Reads the form as it is now, not as it was at the click: what the report
      // says it replaced is what the operator has stated by the time values arrive.
      const fill = typicalFill(state.values, action.response);
      const typical = new Map(state.typical);
      for (const [key, mark] of Object.entries(fill.marks)) typical.set(key as FormKey, mark);
      return {
        ...state,
        values: { ...state.values, ...fill.values },
        typical,
        remembered: withoutKeys(state.remembered, Object.keys(fill.values) as FormKey[]),
        typicalState: { kind: "done", fill },
      };
    }
  }
}

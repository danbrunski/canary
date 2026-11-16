// The sections of the Simulate form (R28.5) that carry their own behaviour:
// the remembered-values banner, the presets, the typical-values button and its
// report, and the grid edges. The fields and the choices that are plain
// inputs stay in `SimForm`, which holds the layout.

import {
  LOAD_MAX,
  LOAD_MIN,
  formatEdges,
  trimLoadEdges,
  type BreakpointSuggestion,
  type FormKey,
  type SimFormValues,
} from "../../../lib/romSim";
import type { SimFormState, TypicalState } from "../../../lib/romSimForm";
import {
  PRESET_DESCRIPTION,
  PRESET_LABEL,
  PRESET_SOURCE,
  describeFill,
  type PresetFill,
  type SimFormReadout,
  type SimPreset,
} from "../../../lib/romSimSize";
import {
  NOT_TOUCHED,
  pedalFlagLine,
  placeholderLine,
  typicalHeadline,
} from "../../../lib/romSimTypical";
import { buttonClass, cn, inputClass } from "../../../styles";

/** "N values were remembered from your last run … Clear remembered values". */
export function SimRememberedBanner({
  count,
  savedAt,
  onClear,
}: {
  count: number;
  savedAt: string | null;
  onClear: () => void;
}) {
  if (count === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-warn/60 p-2 text-sm">
      <span>
        {count} {count === 1 ? "value was" : "values were"} remembered from your last run
        {savedAt ? ` (${savedAt.slice(0, 16).replace("T", " ")} UTC)` : ""} and are marked below.
        Review them: they are sent as shown.
      </span>
      <button type="button" className={buttonClass} onClick={onClear}>
        Clear remembered values
      </button>
    </div>
  );
}

/** Quick check or thorough run: the Coarse and Fine presets and what is
 *  filled. */
export function SimPresets({
  mode,
  applied,
  fills,
  onApply,
}: {
  mode: SimFormReadout["mode"];
  applied: PresetFill | null;
  fills: Record<SimPreset, PresetFill | null>;
  onApply: (preset: SimPreset) => void;
}) {
  return (
    <fieldset
      aria-label="Quick check or thorough run"
      className="space-y-3 rounded-lg border border-border p-3"
    >
      <legend className="px-1 text-sm font-semibold">Quick check or thorough run</legend>
      <p className="text-xs text-fg-dim">
        A preset fills the grid edges and the draws below, visibly, and nothing else. It is not a
        default and is not remembered: press one each time, or state the fields yourself.{" "}
        {PRESET_SOURCE}
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        {(["coarse", "fine"] as const).map((preset) => (
          <div key={preset} className="flex flex-col items-start gap-1">
            <button
              type="button"
              className={cn(buttonClass, mode === preset && "border-accent-2")}
              aria-pressed={mode === preset}
              aria-describedby={`sim-preset-${preset}`}
              disabled={fills[preset] === null}
              onClick={() => onApply(preset)}
            >
              {PRESET_LABEL[preset]}
            </button>
            <span id={`sim-preset-${preset}`} className="text-xs text-fg-dim">
              {PRESET_DESCRIPTION[preset]}
              {fills[preset]
                ? ` On this base: ${fills[preset]?.points.toLocaleString("en-US")} points × ${fills[preset]?.draws.toLocaleString("en-US")} draws.`
                : " This image gives no Fuel Comp breakpoints to build the edges from."}
            </span>
          </div>
        ))}
      </div>
      <p role="status" aria-label="Preset state" className="text-xs">
        {mode === null && (
          <span className="text-fg-faint">
            No preset pressed: the edges and draws are as you stated or remembered them.
          </span>
        )}
        {mode === "custom" && applied && (
          <span className="text-warn">
            <span className="font-semibold">Custom:</span> you edited a field{" "}
            {PRESET_LABEL[applied.preset]} filled, so the edges and draws below are yours.
          </span>
        )}
        {(mode === "coarse" || mode === "fine") && applied && <span>{describeFill(applied)}</span>}
      </p>
    </fieldset>
  );
}

/** "Fill typical values" (W09.11): the button and the report of the last click. */
export function SimTypicalValues({ state, onFill }: { state: TypicalState; onFill: () => void }) {
  return (
    <fieldset aria-label="Typical values" className="space-y-3 rounded-lg border border-border p-3">
      <legend className="px-1 text-sm font-semibold">Typical values for this vehicle</legend>
      <p className="text-xs text-fg-dim">
        Fills the stated environment and the learned state below with the values this vehicle
        declares as typical, each marked with its kind and where it comes from. A value marked
        placeholder is a number filled so the form can run, not a measurement. It is a click, not a
        default: it overwrites only the fields it fills (and lists any value of yours it replaced),
        and never touches the grid, the candidate events, the edges, the draws, the seed or the
        label. Edit any value afterwards, and its mark goes.
      </p>
      <button
        type="button"
        className={buttonClass}
        disabled={state.kind === "loading"}
        onClick={onFill}
      >
        {state.kind === "loading" ? "Fetching typical values…" : "Fill typical values"}
      </button>
      <div role="status" aria-label="Typical values state" className="space-y-1 text-xs">
        {state.kind === "failed" && (
          <div className="space-y-1 text-warn">
            <p className="font-semibold">
              {state.failure.title}
              {state.failure.reason ? ` (${state.failure.reason})` : ""}.
            </p>
            <p>{state.failure.message}</p>
            <p className="text-fg-dim">The form was not changed.</p>
          </div>
        )}
        {state.kind === "done" && (
          <div className="space-y-1">
            <p>{typicalHeadline(state.fill)}</p>
            {state.fill.filled.length > 0 && (
              <ul aria-label="Filled typical values" className="list-inside list-disc">
                {state.fill.filled.map((item) => (
                  <li key={item.field}>
                    {item.label}: <span className="font-num">{item.shown}</span>
                    {item.kind === "placeholder" && (
                      <span className="font-semibold text-warn"> (placeholder)</span>
                    )}
                    {item.replaced !== null && (
                      <span className="text-warn">
                        {" "}
                        (replaced your <span className="font-num">{item.replaced}</span>)
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {placeholderLine(state.fill) !== null && (
              <p className="text-warn">{placeholderLine(state.fill)}</p>
            )}
            {state.fill.pedalFlag !== null && <p>{pedalFlagLine(state.fill.pedalFlag)}</p>}
            {state.fill.left.length > 0 && (
              <div>
                <p className="font-semibold text-warn">
                  Left empty for you to state ({state.fill.left.length}):
                </p>
                <ul aria-label="Left empty" className="list-inside list-disc">
                  {state.fill.left.map((item) => (
                    <li key={item.field}>
                      {item.label}: {item.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {state.fill.skipped.length > 0 && (
              <div>
                <p className="font-semibold text-warn">Not filled ({state.fill.skipped.length}):</p>
                <ul aria-label="Not filled" className="list-inside list-disc">
                  {state.fill.skipped.map((item) => (
                    <li key={item.field}>
                      {item.label}: {item.why}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <p className="text-fg-dim">{NOT_TOUCHED}</p>
          </div>
        )}
      </div>
    </fieldset>
  );
}

/** The grid edges: the two edge lists, the grid size, Reset and Trim, and the
 *  note on where the suggested edges came from. */
export function SimEdges({
  values,
  suggestion,
  typedEdges,
  readout,
  invalidOf,
  onChange,
  onReset,
}: {
  values: SimFormValues;
  suggestion: BreakpointSuggestion;
  typedEdges: SimFormState["typedEdges"];
  readout: SimFormReadout;
  invalidOf: (key: string) => string | null;
  onChange: (key: FormKey, value: string) => void;
  onReset: () => void;
}) {
  const { load, loadOutside, gridSizeText, uniform } = readout;
  const rpmProblem = invalidOf("rpmEdges");
  const loadProblem = invalidOf("loadEdges");
  return (
    <fieldset className="space-y-3 rounded-lg border border-border p-3">
      <legend className="px-1 text-sm font-semibold">Grid edges</legend>
      <p className="text-xs text-fg-dim">
        One design point at each cell centre. The suggestion is the midpoints between the base
        image&apos;s Fuel Compensation (Map 1) breakpoints, extended half a step at each end, read
        by what each axis decodes to (rpm, throttle-model fill), not by rows or columns: UpRev
        labels them the other way round. Edit freely.
      </p>
      <div className="flex flex-wrap gap-3">
        <div className="flex min-w-60 flex-1 flex-col gap-1 text-sm">
          <label htmlFor="sim-rpm-edges">RPM edges (rpm)</label>
          <input
            id="sim-rpm-edges"
            className={cn(inputClass, "font-num")}
            autoComplete="off"
            value={values.rpmEdges}
            aria-required="true"
            aria-invalid={rpmProblem !== null || undefined}
            onChange={(event) => onChange("rpmEdges", event.target.value)}
          />
          {rpmProblem !== null && <span className="text-xs text-bad">{rpmProblem}</span>}
        </div>
        <div className="flex min-w-60 flex-1 flex-col gap-1 text-sm">
          <label htmlFor="sim-load-edges">Load edges (% fill)</label>
          <input
            id="sim-load-edges"
            className={cn(inputClass, "font-num")}
            autoComplete="off"
            value={values.loadEdges}
            aria-required="true"
            aria-invalid={loadProblem !== null || undefined}
            onChange={(event) => onChange("loadEdges", event.target.value)}
          />
          {loadProblem !== null && <span className="text-xs text-bad">{loadProblem}</span>}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <span aria-label="Grid size" className="font-num">
          {gridSizeText}
        </span>
        {suggestion.rpmEdges && suggestion.loadEdges && (
          <button type="button" className={buttonClass} onClick={onReset}>
            Reset to the base&apos;s breakpoints
          </button>
        )}
        {loadOutside && (
          <button
            type="button"
            className={buttonClass}
            onClick={() => onChange("loadEdges", formatEdges(trimLoadEdges(load.edges)))}
          >
            Trim load edges to {LOAD_MIN}–{LOAD_MAX} %
          </button>
        )}
      </div>
      <p className="text-xs text-fg-faint">
        {suggestion.rpmEdges && !typedEdges.rpm
          ? "RPM edges: suggested from the base's Fuel Comp rpm breakpoints. "
          : ""}
        {suggestion.loadEdges && !typedEdges.load
          ? "Load edges: suggested from the base's Fuel Comp load breakpoints. "
          : ""}
        {suggestion.notes.join("; ")}
        {suggestion.rpmEdges && suggestion.loadEdges && !uniform
          ? ` The axes are not uniform, so a cell centre sits up to ${(Math.max(suggestion.offCentre.rpm, suggestion.offCentre.load) * 100).toFixed(0)} % of a cell off its breakpoint.`
          : ""}
      </p>
    </fieldset>
  );
}

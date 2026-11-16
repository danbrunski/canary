import { useSimFormState } from "../../../hooks/useSimFormState";
import {
  CANDIDATE_EVENT_CHOICES,
  DRAWS_FIELD,
  ENVIRONMENT_FIELDS,
  GRID_CHOICES,
  HIGH_DET_CHOICES,
  highDetRuledOut,
  KNOCK_CONTROL_CHOICES,
  LEARNED_FIELDS,
  LOAD_BASIS,
  LOAD_BASIS_MEANING,
  MAX_DRAWS,
  RAIL_FIELD,
  SEED_FIELD,
  newSeed,
  saveRemembered,
} from "../../../lib/romSim";
import { COST_FIT, type TimedRun } from "../../../lib/romSimSize";
import { buttonClass, cn, selectClass } from "../../../styles";
import type { RomEdit, RomSimRequest } from "../../../types/romEditor";
import type { RomTableValues } from "../../../types/romLibrary";
import { Choice, NumberInput, Remembered, Typical } from "./SimFields";
import { SimEdges, SimPresets, SimRememberedBanner, SimTypicalValues } from "./SimFormSections";

/** The Simulate form (W09.4). Nothing is defaulted: every field starts empty
 *  (the suggested edges excepted, which are derived from the base image and
 *  shown with their source), values remembered from the last run come back
 *  marked, and Run names whatever is missing. */
export function SimForm({
  token,
  sha,
  tables,
  label,
  edits,
  blocked,
  startedAt,
  timings,
  onRun,
}: {
  /** The control token, for the typical-values request only. */
  token: string;
  /** The base image: its vehicle's typical values are the ones offered. */
  sha: string;
  tables: RomTableValues[];
  label: string;
  edits: RomEdit[];
  blocked: { reason: string; text: string } | null;
  /** Set while a request is in flight. */
  startedAt: number | null;
  /** This server's earlier runs (`romSimSize.loadTimings`), for the estimate. */
  timings: TimedRun[];
  onRun: (request: RomSimRequest) => void;
}) {
  const form = useSimFormState({ token, sha, tables, label, edits, timings });
  const { state, readout, suggestion, change } = form;
  const { values, remembered, typical } = state;
  const { check, missing, invalid, wotWarning, drawsStated, cost, sizeVerdict } = readout;
  const invalidOf = (key: string) => {
    const found = check.problems.find((item) => item.field === key);
    return found?.kind === "invalid" ? found.message : null;
  };
  const running = startedAt !== null;
  const disabled = blocked !== null;
  const railMark = typical.get("rail");

  function run() {
    if (!check.request || disabled || running) return;
    saveRemembered(values);
    onRun(check.request);
  }

  return (
    <form
      aria-label="Simulate form"
      className="space-y-4"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        run();
      }}
    >
      {blocked && (
        <p role="note" className="text-sm text-warn">
          Simulate is unavailable: {blocked.reason}. {blocked.text}
        </p>
      )}
      <fieldset disabled={disabled} className="space-y-4 disabled:opacity-60">
        <p className="text-sm text-fg-dim">
          Runs the pending edits against this base over a stated grid, base and candidate paired on
          every draw. Every value below is stated by you: nothing is defaulted, and a value from
          your last run is shown and marked, not applied unseen. It is one bounded job on the
          daemon, refused while a capture records.
        </p>

        <SimRememberedBanner
          count={remembered.size}
          savedAt={state.savedAt}
          onClear={form.clearRemembered}
        />

        <Choice
          legend="Aggregation grid"
          name="sim-grid"
          choices={GRID_CHOICES}
          value={values.grid}
          onChange={(id) => change("grid", id)}
        />

        <SimPresets
          mode={readout.mode}
          applied={state.applied}
          fills={form.fills}
          onApply={form.applyPreset}
        />

        <SimTypicalValues state={state.typicalState} onFill={() => void form.fillTypical()} />

        <SimEdges
          values={values}
          suggestion={suggestion}
          typedEdges={state.typedEdges}
          readout={readout}
          invalidOf={invalidOf}
          onChange={change}
          onReset={form.resetEdges}
        />

        <fieldset className="space-y-3 rounded-lg border border-border p-3">
          <legend className="px-1 text-sm font-semibold">Environment (stated)</legend>
          <div className="flex flex-wrap gap-3">
            {ENVIRONMENT_FIELDS.map((spec) => (
              <NumberInput
                key={spec.key}
                spec={spec}
                values={values}
                remembered={remembered}
                typical={typical}
                problem={invalidOf(spec.key)}
                onChange={change}
              />
            ))}
            <div className="flex min-w-48 flex-1 flex-col gap-1 text-sm">
              <label htmlFor="sim-rail">Rail pressure</label>
              <select
                id="sim-rail"
                className={cn(
                  selectClass,
                  (remembered.has("rail") || typical.has("rail")) && "border-warn",
                )}
                value={values.rail}
                aria-required="true"
                aria-describedby={
                  typical.has("rail")
                    ? "sim-rail-typical"
                    : remembered.has("rail")
                      ? "sim-rail-remembered"
                      : undefined
                }
                onChange={(event) => change("rail", event.target.value)}
              >
                <option value="">Choose…</option>
                <option value="working_assumption">The plant&apos;s working assumption</option>
                <option value="stated">A stated value (kPa)</option>
              </select>
              {remembered.has("rail") && !typical.has("rail") && (
                <Remembered id="sim-rail-remembered" />
              )}
              {railMark && <Typical id="sim-rail-typical" mark={railMark} />}
              {values.rail === "working_assumption" && (
                <span className="text-xs text-fg-faint">
                  Runs the plant&apos;s own rail value, and the result says so.
                </span>
              )}
            </div>
            {values.rail === "stated" && (
              <NumberInput
                spec={RAIL_FIELD}
                values={values}
                remembered={remembered}
                typical={typical}
                problem={invalidOf("railKpa")}
                onChange={change}
              />
            )}
          </div>
        </fieldset>

        <fieldset className="space-y-3 rounded-lg border border-border p-3">
          <legend className="px-1 text-sm font-semibold">
            Learned state (both ROMs start here)
          </legend>
          <div className="flex flex-wrap gap-3">
            {LEARNED_FIELDS.map((spec) => (
              <NumberInput
                key={spec.key}
                spec={spec}
                values={values}
                remembered={remembered}
                typical={typical}
                problem={invalidOf(spec.key)}
                onChange={change}
              />
            ))}
          </div>
        </fieldset>

        <fieldset
          aria-label="Ignition timing state"
          className="space-y-3 rounded-lg border border-border p-3"
        >
          <legend className="px-1 text-sm font-semibold">Ignition timing state (stated)</legend>
          <p className="text-xs text-fg-dim">
            Nothing logs the knock controller&apos;s HI DET latch or its fallback state, and both
            change when the knock sensor is repaired (on 2026-10-08 the truck read the latch set and
            knock control in fallback, the KS1 fault). So they have no default, no typical value,
            and are not remembered: state the condition you are asking about. The latch picks the
            trim the ECM reads warm, so it decides whether an edit to trim A or to trim B shows in
            the timing result. Fallback with the latch clear is not a running state (the fault sets
            the latch), so choosing fallback sets the latch and clear is unavailable with it.
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            <Choice
              legend="HI DET latch"
              name="sim-high-det"
              choices={HIGH_DET_CHOICES}
              value={values.highDet}
              onChange={(id) => change("highDet", id)}
              ruledOut={highDetRuledOut(values.knockControl)}
            />
            <Choice
              legend="Knock control"
              name="sim-knock-control"
              choices={KNOCK_CONTROL_CHOICES}
              value={values.knockControl}
              onChange={(id) => change("knockControl", id)}
            />
          </div>
        </fieldset>

        <Choice
          legend="Candidate events"
          name="sim-events"
          choices={CANDIDATE_EVENT_CHOICES}
          value={values.candidateEvents}
          onChange={(id) => change("candidateEvents", id)}
        />

        <div className="rounded-lg border border-border p-3 text-sm">
          <p>
            <span className="font-semibold">Load basis:</span>{" "}
            <span className="font-num">{LOAD_BASIS}</span> (the only value)
          </p>
          <p className="text-xs text-fg-dim">{LOAD_BASIS_MEANING}</p>
        </div>

        <fieldset className="space-y-3 rounded-lg border border-border p-3">
          <legend className="px-1 text-sm font-semibold">Sampling</legend>
          <div className="flex flex-wrap gap-3">
            <NumberInput
              spec={{
                ...DRAWS_FIELD,
                hint: `${drawsStated} of at most ${MAX_DRAWS.toLocaleString("en-US")} (draws_too_many); the cost on the Pi is not yet measured`,
              }}
              values={values}
              remembered={remembered}
              typical={typical}
              problem={invalidOf("draws")}
              onChange={change}
            />
            <NumberInput
              spec={{ ...SEED_FIELD, hint: "the same seed reproduces a run" }}
              values={values}
              remembered={remembered}
              typical={typical}
              problem={invalidOf("seed")}
              onChange={change}
              action={
                <button
                  type="button"
                  className={buttonClass}
                  onClick={() => change("seed", String(newSeed()))}
                >
                  New seed
                </button>
              }
            />
          </div>
        </fieldset>

        <div className="space-y-2">
          <p className="text-sm text-fg-dim">
            {edits.length} pending {edits.length === 1 ? "edit" : "edits"}
            {label.trim() ? ` as "${label.trim()}"` : ""}.
          </p>
          {wotWarning && (
            <p role="note" aria-label="WOT column warning" className="text-sm text-warn">
              {wotWarning}
            </p>
          )}
          <button
            type="submit"
            className={buttonClass}
            disabled={disabled || running || check.request === null}
            title={
              check.request === null
                ? `Run needs: ${missing.map((item) => item.label).join(", ") || "valid values"}`
                : undefined
            }
          >
            Run simulation
          </button>
          <div aria-label="Cost estimate" role="group" className="space-y-1 text-sm">
            {cost ? (
              <>
                <p className="font-num">{cost.headline}</p>
                <p className="text-xs text-fg-dim">{cost.detail}</p>
                {sizeVerdict?.coarse && (
                  <p className="text-xs text-warn">
                    Below the Fine preset ({sizeVerdict.reasons.join("; ")}): the result will carry
                    a coarse-run banner.
                  </p>
                )}
              </>
            ) : (
              <p className="text-xs text-fg-dim">
                Cost: state the edges and the draws to see points × draws and the time. The fit is{" "}
                {COST_FIT.overheadS.toFixed(1)} s + {COST_FIT.secondsPerPointDraw.toExponential(1)}{" "}
                s × points × draws on the dev laptop.
              </p>
            )}
          </div>
          {missing.length > 0 && (
            <p role="status" aria-label="What blocks Run" className="text-sm text-fg-dim">
              Run needs: {missing.map((item) => item.label).join(", ")}.
            </p>
          )}
          {invalid.length > 0 && (
            <ul aria-label="Values to fix" className="list-inside list-disc text-sm text-bad">
              {invalid.map((item) => (
                <li key={item.field}>
                  {item.label}: {item.message}
                </li>
              ))}
            </ul>
          )}
        </div>
      </fieldset>
    </form>
  );
}

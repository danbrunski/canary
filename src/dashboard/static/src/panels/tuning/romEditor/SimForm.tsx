import { useEffect, useId, useMemo, useState, type ReactNode } from "react";

import {
  CANDIDATE_EVENT_CHOICES,
  DEFERRED_ADVICE,
  DRAWS_FIELD,
  ENVIRONMENT_FIELDS,
  FAILURE_TITLE,
  GRID_CHOICES,
  LEARNED_FIELDS,
  LOAD_BASIS,
  LOAD_BASIS_MEANING,
  LOAD_MAX,
  LOAD_MIN,
  MAX_DRAWS,
  MAX_GRID_POINTS,
  RAIL_FIELD,
  SEED_FIELD,
  checkSimForm,
  EMPTY_FORM,
  forgetRemembered,
  formatEdges,
  gridPoints,
  loadEdgesError,
  loadRemembered,
  newSeed,
  parseEdges,
  parseNumber,
  saveRemembered,
  seedForm,
  suggestEdges,
  trimLoadEdges,
  type FormKey,
  type NumberFieldSpec,
  type SimFormValues,
} from "../../../lib/romSim";
import { buttonClass, cn, inputClass, selectClass } from "../../../styles";
import type { RomEdit, RomSimRequest, SimFailure } from "../../../types/romEditor";
import type { RomTableValues } from "../../../types/romLibrary";

/** A short "remembered" tag next to a field whose value came back from the
 *  browser's storage. Shown, so a remembered value is never applied unseen. */
function Remembered({ id }: { id: string }) {
  return (
    <span id={id} className="text-xs text-warn" data-remembered="true">
      remembered from your last run
    </span>
  );
}

function NumberInput({
  spec,
  values,
  remembered,
  problem,
  onChange,
  action,
}: {
  spec: NumberFieldSpec;
  values: SimFormValues;
  remembered: ReadonlySet<FormKey>;
  problem: string | null;
  onChange: (key: FormKey, value: string) => void;
  action?: ReactNode;
}) {
  const id = useId();
  const name = spec.unit ? `${spec.label} (${spec.unit})` : spec.label;
  const marked = remembered.has(spec.key);
  return (
    <div className="flex min-w-40 flex-1 flex-col gap-1 text-sm">
      <label htmlFor={id}>{name}</label>
      <div className="flex gap-2">
        <input
          id={id}
          className={cn(inputClass, "min-w-0 flex-1 font-num", marked && "border-warn")}
          inputMode="decimal"
          autoComplete="off"
          value={values[spec.key]}
          aria-required="true"
          aria-invalid={problem ? true : undefined}
          aria-describedby={marked ? `${id}-remembered` : undefined}
          onChange={(event) => onChange(spec.key, event.target.value)}
        />
        {action}
      </div>
      {marked && <Remembered id={`${id}-remembered`} />}
      {spec.hint && <span className="text-xs text-fg-faint">{spec.hint}</span>}
      {problem && <span className="text-xs text-bad">{problem}</span>}
    </div>
  );
}

function Choice<T extends string>({
  legend,
  name,
  choices,
  value,
  onChange,
}: {
  legend: string;
  name: string;
  choices: readonly { id: T; label: string; meaning: string }[];
  value: "" | T;
  onChange: (id: T) => void;
}) {
  return (
    <fieldset className="space-y-1.5 rounded-lg border border-border p-3">
      <legend className="px-1 text-sm font-semibold">{legend}</legend>
      {choices.map((choice) => (
        <label key={choice.id} className="flex cursor-pointer items-start gap-2 text-sm">
          <input
            type="radio"
            name={name}
            className="mt-1"
            value={choice.id}
            checked={value === choice.id}
            onChange={() => onChange(choice.id)}
          />
          <span>
            {choice.label}
            <span className="block text-xs text-fg-dim">{choice.meaning}</span>
          </span>
        </label>
      ))}
      {value === "" && <p className="text-xs text-fg-faint">Choose one: there is no default.</p>}
    </fieldset>
  );
}

/** Seconds since `startedAt`, ticking while the component is mounted. */
function useElapsedSeconds(startedAt: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, []);
  return Math.max(0, Math.floor((now - startedAt) / 1000));
}

/** One request in flight: a spinner and the time it has taken. */
export function SimRunning({ startedAt }: { startedAt: number }) {
  const elapsed = useElapsedSeconds(startedAt);
  return (
    <div
      role="status"
      className="flex items-center gap-3 rounded-lg border border-border p-3 text-sm"
    >
      <span
        aria-hidden="true"
        className="inline-block size-4 animate-spin rounded-full border-2 border-fg-faint border-t-accent-2"
      />
      <span>
        Simulating… <span className="font-num">{elapsed} s</span> elapsed. One request, one job on
        the daemon; it can take minutes.
      </span>
    </div>
  );
}

/** A failed run by its reason: a refusal names the reason, a deferred run says
 *  to take the images to the laptop. */
export function SimFailureAlert({ failure }: { failure: SimFailure }) {
  return (
    <div role="alert" className="space-y-1 rounded-lg border border-bad/55 p-3 text-sm">
      <p className="font-semibold text-bad">
        {FAILURE_TITLE[failure.kind]}
        {failure.reason ? `: ${failure.reason}` : ""}
      </p>
      {failure.kind === "deferred" && <p>{DEFERRED_ADVICE}</p>}
      <p>{failure.message}</p>
      {failure.kind === "deferred" && failure.laptopPath && (
        <p className="text-fg-dim">{failure.laptopPath}</p>
      )}
      {failure.kind === "busy" && (
        <p className="text-fg-dim">
          Another analysis or digest holds the single workbench slot. Run again when it is done.
        </p>
      )}
      {failure.refusalKind && (
        <p className="text-xs text-fg-faint">refusal class: {failure.refusalKind}</p>
      )}
    </div>
  );
}

/** The Simulate form (W09.4). Nothing is defaulted: every field starts empty
 *  (the suggested edges excepted, which are derived from the base image and
 *  shown with their source), values remembered from the last run come back
 *  marked, and Run names whatever is missing. */
export function SimForm({
  tables,
  label,
  edits,
  blocked,
  startedAt,
  onRun,
}: {
  tables: RomTableValues[];
  label: string;
  edits: RomEdit[];
  blocked: { reason: string; text: string } | null;
  /** Set while a request is in flight. */
  startedAt: number | null;
  onRun: (request: RomSimRequest) => void;
}) {
  const suggestion = useMemo(() => suggestEdges(tables), [tables]);
  const [seeded] = useState(() => {
    const base: SimFormValues = {
      ...EMPTY_FORM,
      rpmEdges: suggestion.rpmEdges ? formatEdges(suggestion.rpmEdges) : "",
      loadEdges: suggestion.loadEdges ? formatEdges(suggestion.loadEdges) : "",
    };
    const remembered = loadRemembered();
    return { ...seedForm(base, remembered), savedAt: remembered.savedAt };
  });
  const [values, setValues] = useState<SimFormValues>(seeded.values);
  const [remembered, setRemembered] = useState<ReadonlySet<FormKey>>(new Set(seeded.remembered));
  const [savedAt, setSavedAt] = useState<string | null>(
    seeded.remembered.length > 0 ? seeded.savedAt : null,
  );
  // The edges say where they came from until the operator types.
  const [typedEdges, setTypedEdges] = useState<{ rpm: boolean; load: boolean }>({
    rpm: false,
    load: false,
  });

  function change(key: FormKey, value: string) {
    setValues((current) => ({ ...current, [key]: value }));
    setRemembered((current) => {
      if (!current.has(key)) return current;
      const next = new Set(current);
      next.delete(key);
      return next;
    });
    if (key === "rpmEdges") setTypedEdges((current) => ({ ...current, rpm: true }));
    if (key === "loadEdges") setTypedEdges((current) => ({ ...current, load: true }));
  }

  const check = useMemo(() => checkSimForm(values, label, edits), [values, label, edits]);
  const problemOf = (key: string) => check.problems.find((item) => item.field === key);
  const invalidOf = (key: string) => {
    const found = problemOf(key);
    return found?.kind === "invalid" ? found.message : null;
  };
  const rpm = parseEdges(values.rpmEdges);
  const load = parseEdges(values.loadEdges);
  const cells = gridPoints(rpm.error ? [] : rpm.edges, load.error ? [] : load.edges);
  const missing = check.problems.filter((item) => item.kind === "missing");
  const invalid = check.problems.filter((item) => item.kind === "invalid");
  const running = startedAt !== null;
  const disabled = blocked !== null;
  const loadOutside = !load.error && load.edges.length > 1 && loadEdgesError(load.edges) !== null;
  const drawsParsed = parseNumber(values.draws, { integer: true, min: 1, max: null });
  const drawsStated = drawsParsed.kind === "ok" ? String(drawsParsed.value) : "not stated";
  const uniform = suggestion.offCentre.rpm <= 0.05 && suggestion.offCentre.load <= 0.05;

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

        {remembered.size > 0 && (
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-warn/60 p-2 text-sm">
            <span>
              {remembered.size} {remembered.size === 1 ? "value was" : "values were"} remembered
              from your last run
              {savedAt ? ` (${savedAt.slice(0, 16).replace("T", " ")} UTC)` : ""} and are marked
              below. Review them: they are sent as shown.
            </span>
            <button
              type="button"
              className={buttonClass}
              onClick={() => {
                forgetRemembered();
                setValues((current) => {
                  const next = { ...current };
                  for (const key of remembered) (next as Record<FormKey, string>)[key] = "";
                  return next;
                });
                setRemembered(new Set());
                setSavedAt(null);
              }}
            >
              Clear remembered values
            </button>
          </div>
        )}

        <Choice
          legend="Aggregation grid"
          name="sim-grid"
          choices={GRID_CHOICES}
          value={values.grid}
          onChange={(id) => change("grid", id)}
        />

        <fieldset className="space-y-3 rounded-lg border border-border p-3">
          <legend className="px-1 text-sm font-semibold">Grid edges</legend>
          <p className="text-xs text-fg-dim">
            One design point at each cell centre. The suggestion is the midpoints between the base
            image&apos;s Fuel Compensation (Map 1) breakpoints, extended half a step at each end,
            read by what each axis decodes to (rpm, throttle-model fill), not by rows or columns:
            UpRev labels them the other way round. Edit freely.
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
                aria-invalid={problemOf("rpmEdges")?.kind === "invalid" || undefined}
                onChange={(event) => change("rpmEdges", event.target.value)}
              />
              {problemOf("rpmEdges")?.kind === "invalid" && (
                <span className="text-xs text-bad">{problemOf("rpmEdges")?.message}</span>
              )}
            </div>
            <div className="flex min-w-60 flex-1 flex-col gap-1 text-sm">
              <label htmlFor="sim-load-edges">Load edges (% fill)</label>
              <input
                id="sim-load-edges"
                className={cn(inputClass, "font-num")}
                autoComplete="off"
                value={values.loadEdges}
                aria-required="true"
                aria-invalid={problemOf("loadEdges")?.kind === "invalid" || undefined}
                onChange={(event) => change("loadEdges", event.target.value)}
              />
              {problemOf("loadEdges")?.kind === "invalid" && (
                <span className="text-xs text-bad">{problemOf("loadEdges")?.message}</span>
              )}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span aria-label="Grid size" className="font-num">
              {rpm.error || load.error || rpm.empty || load.empty
                ? `grid size: not stated (at most ${MAX_GRID_POINTS.toLocaleString("en-US")} cells)`
                : `${rpm.edges.length - 1} × ${load.edges.length - 1} = ${cells} of at most ${MAX_GRID_POINTS.toLocaleString("en-US")} cells`}
            </span>
            {suggestion.rpmEdges && suggestion.loadEdges && (
              <button
                type="button"
                className={buttonClass}
                onClick={() => {
                  setValues((current) => ({
                    ...current,
                    rpmEdges: formatEdges(suggestion.rpmEdges ?? []),
                    loadEdges: formatEdges(suggestion.loadEdges ?? []),
                  }));
                  setTypedEdges({ rpm: false, load: false });
                }}
              >
                Reset to the base&apos;s breakpoints
              </button>
            )}
            {loadOutside && (
              <button
                type="button"
                className={buttonClass}
                onClick={() => change("loadEdges", formatEdges(trimLoadEdges(load.edges)))}
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

        <fieldset className="space-y-3 rounded-lg border border-border p-3">
          <legend className="px-1 text-sm font-semibold">Environment (stated)</legend>
          <div className="flex flex-wrap gap-3">
            {ENVIRONMENT_FIELDS.map((spec) => (
              <NumberInput
                key={spec.key}
                spec={spec}
                values={values}
                remembered={remembered}
                problem={invalidOf(spec.key)}
                onChange={change}
              />
            ))}
            <div className="flex min-w-48 flex-1 flex-col gap-1 text-sm">
              <label htmlFor="sim-rail">Rail pressure</label>
              <select
                id="sim-rail"
                className={cn(selectClass, remembered.has("rail") && "border-warn")}
                value={values.rail}
                aria-required="true"
                onChange={(event) => change("rail", event.target.value)}
              >
                <option value="">Choose…</option>
                <option value="working_assumption">The plant&apos;s working assumption</option>
                <option value="stated">A stated value (kPa)</option>
              </select>
              {remembered.has("rail") && <Remembered id="sim-rail-remembered" />}
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
                problem={invalidOf(spec.key)}
                onChange={change}
              />
            ))}
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
              problem={invalidOf("draws")}
              onChange={change}
            />
            <NumberInput
              spec={{ ...SEED_FIELD, hint: "the same seed reproduces a run" }}
              values={values}
              remembered={remembered}
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

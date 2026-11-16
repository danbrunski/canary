import { useId, type ReactNode } from "react";

import type { FormKey, NumberFieldSpec, SimFormValues } from "../../../lib/romSim";
import { typicalTag, type TypicalMark } from "../../../lib/romSimTypical";
import { cn, inputClass } from "../../../styles";

/** A short "remembered" tag next to a field whose value came back from the
 *  browser's storage. Shown, so a remembered value is never applied unseen. */
export function Remembered({ id }: { id: string }) {
  return (
    <span id={id} className="text-xs text-warn" data-remembered="true">
      remembered from your last run
    </span>
  );
}

/** The tag next to a field the "Fill typical values" button filled: its kind
 *  (`typical`, or `placeholder` for a number the simulator reads nowhere) and its
 *  source, from the vehicle's data. Shown, so a typical value is never applied
 *  unseen and a placeholder is never read as a measurement; it goes when the
 *  operator edits the field. */
export function Typical({ id, mark }: { id: string; mark: TypicalMark }) {
  return (
    <span
      id={id}
      className={cn(
        "max-w-prose break-words text-xs text-warn",
        mark.kind === "placeholder" && "font-semibold",
      )}
      data-typical="true"
      data-typical-kind={mark.kind}
    >
      {typicalTag(mark)}
    </span>
  );
}

export function NumberInput({
  spec,
  values,
  remembered,
  typical,
  problem,
  onChange,
  action,
}: {
  spec: NumberFieldSpec;
  values: SimFormValues;
  remembered: ReadonlySet<FormKey>;
  /** Fields the typical-values button filled, with the kind and source each shows. */
  typical: ReadonlyMap<FormKey, TypicalMark>;
  problem: string | null;
  onChange: (key: FormKey, value: string) => void;
  action?: ReactNode;
}) {
  const id = useId();
  const name = spec.unit ? `${spec.label} (${spec.unit})` : spec.label;
  const typicalMark = typical.get(spec.key);
  const marked = remembered.has(spec.key) || typicalMark !== undefined;
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
          aria-describedby={
            typicalMark !== undefined
              ? `${id}-typical`
              : remembered.has(spec.key)
                ? `${id}-remembered`
                : undefined
          }
          onChange={(event) => onChange(spec.key, event.target.value)}
        />
        {action}
      </div>
      {remembered.has(spec.key) && typicalMark === undefined && (
        <Remembered id={`${id}-remembered`} />
      )}
      {typicalMark !== undefined && <Typical id={`${id}-typical`} mark={typicalMark} />}
      {spec.hint && <span className="text-xs text-fg-faint">{spec.hint}</span>}
      {problem && <span className="text-xs text-bad">{problem}</span>}
    </div>
  );
}

export function Choice<T extends string>({
  legend,
  name,
  choices,
  value,
  onChange,
  ruledOut = {},
}: {
  legend: string;
  name: string;
  choices: readonly { id: T; label: string; meaning: string }[];
  value: "" | T;
  onChange: (id: T) => void;
  /** Choices another answer rules out, each with the one-line why; disabled. */
  ruledOut?: Partial<Record<T, string>>;
}) {
  return (
    <fieldset className="space-y-1.5 rounded-lg border border-border p-3">
      <legend className="px-1 text-sm font-semibold">{legend}</legend>
      {choices.map((choice) => {
        const why = ruledOut[choice.id];
        return (
          <label
            key={choice.id}
            className={`flex items-start gap-2 text-sm ${why ? "opacity-60" : "cursor-pointer"}`}
          >
            <input
              type="radio"
              name={name}
              className="mt-1"
              value={choice.id}
              checked={value === choice.id}
              disabled={why !== undefined}
              onChange={() => onChange(choice.id)}
            />
            <span>
              {choice.label}
              <span className="block text-xs text-fg-dim">{choice.meaning}</span>
              {why !== undefined && <span className="block text-xs text-fg-faint">{why}</span>}
            </span>
          </label>
        );
      })}
      {value === "" && <p className="text-xs text-fg-faint">Choose one: there is no default.</p>}
    </fieldset>
  );
}

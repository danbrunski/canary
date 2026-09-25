import { buttonClass, cn } from "../styles";

export interface SegmentOption<T extends string> {
  id: T;
  label: string;
}

/** A one-of-N switch (R08.11). AnalysisPage and LogsPage each re-rolled one,
 *  with different accessibility: `role="tab"`/`aria-selected` in one place,
 *  `aria-pressed` in another. This is the one pattern: a labelled group of
 *  toggle buttons, the chosen one `aria-pressed`. */
export function SegmentedControl<T extends string>({
  label,
  options,
  value,
  onChange,
  className,
}: {
  /** Names the group for assistive tech ("Analysis views"). */
  label: string;
  options: ReadonlyArray<SegmentOption<T>>;
  value: T;
  onChange: (id: T) => void;
  className?: string;
}) {
  return (
    <div role="group" aria-label={label} className={cn("flex gap-1", className)}>
      {options.map((option) => (
        <button
          key={option.id}
          type="button"
          aria-pressed={value === option.id}
          className={cn(buttonClass, value === option.id && "border-accent text-accent")}
          onClick={() => onChange(option.id)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

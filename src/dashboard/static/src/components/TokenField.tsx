import type { Ref } from "react";

import { buttonClass, cn, inputClass } from "../styles";

interface TokenFieldProps {
  value: string;
  onChange: (value: string) => void;
  /** Extra classes on the `<input>` (e.g. `"w-full"`). */
  className?: string;
  /** Show a "Clear" button once a value is entered. Default true. */
  showClear?: boolean;
  /** Lets a caller focus the input (e.g. when entry is requested). */
  inputRef?: Ref<HTMLInputElement>;
}

/** The control-token password input + its "Clear" button, hand-rendered
 *  divergently in ControlPanel and TestSessionPanel (Task 3.3). Each panel
 *  keeps its own surrounding chrome (label, status badge, help text) —
 *  only the input/clear pair itself is shared. */
export function TokenField({
  value,
  onChange,
  className,
  showClear = true,
  inputRef,
}: TokenFieldProps) {
  return (
    <div className="flex gap-2">
      <input
        ref={inputRef}
        className={cn(inputClass, className)}
        type="password"
        autoComplete="off"
        placeholder="CANARY_CONTROL_TOKEN"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      {showClear && value && (
        <button type="button" className={buttonClass} onClick={() => onChange("")}>
          Clear
        </button>
      )}
    </div>
  );
}

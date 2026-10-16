import { useState } from "react";

import { selectionText } from "../../../lib/romEditor";
import { buttonClass, inputClass } from "../../../styles";
import type { CellSelection, RomEditOperation, RomRounding } from "../../../types/romEditor";
import type { RomTableValues } from "../../../types/romLibrary";

type Mode = RomEditOperation["kind"];

const ROUNDINGS: { value: RomRounding; label: string }[] = [
  { value: "nearest", label: "Nearest (half up)" },
  { value: "floor", label: "Floor" },
  { value: "ceil", label: "Ceiling" },
];

/** Parse a typed number; null for empty or not a number. */
function parse(text: string): number | null {
  if (!text.trim()) return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

/** What to do to the selected cells. "Set to value" is offered only where the
 *  table declares a decode; it is inverted to a raw in the browser and sent
 *  as `set_raw`. A scale has no default rounding. Applying only hands the
 *  operation up: the page builds, checks and lists the edit. */
export function OperationPanel({
  table,
  selection,
  refusal,
  onApply,
}: {
  table: RomTableValues;
  selection: CellSelection | null;
  /** The last refusal from building the edit, by reason. */
  refusal: { reason: string; message: string } | null;
  onApply: (op: RomEditOperation) => void;
}) {
  const hasDecode = Boolean(table.decode);
  const [mode, setMode] = useState<Mode>(hasDecode ? "set_value" : "set_raw");
  const [text, setText] = useState("");
  const [rounding, setRounding] = useState<RomRounding | "">("");
  const active: Mode = mode === "set_value" && !hasDecode ? "set_raw" : mode;
  const number = parse(text);
  const unit = table.decode?.unit ?? "";

  function apply() {
    if (number === null) return;
    switch (active) {
      case "set_value":
        return onApply({ kind: "set_value", value: number });
      case "set_raw":
        return onApply({ kind: "set_raw", raw: number });
      case "add_raw":
        return onApply({ kind: "add_raw", delta: number });
      case "scale":
        return onApply({ kind: "scale", percent: number, rounding: rounding || null });
    }
  }

  const fieldLabel = {
    set_value: `Value (${unit})`,
    set_raw: "Raw count",
    add_raw: "Raw counts to add",
    scale: "Scale (%)",
  }[active];
  const ready = selection !== null && number !== null;

  return (
    <form
      className="space-y-3"
      aria-label="Operation"
      onSubmit={(event) => {
        event.preventDefault();
        apply();
      }}
    >
      <p className="text-sm text-fg-dim" role="status">
        {selection ? `Selected: ${selectionText(selection)}.` : "Select cells in the table first."}
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Operation
          <select
            className={inputClass}
            value={active}
            onChange={(event) => setMode(event.target.value as Mode)}
          >
            {hasDecode && <option value="set_value">Set to value ({unit})</option>}
            <option value="set_raw">Set raw count</option>
            <option value="add_raw">Add raw counts</option>
            <option value="scale">Scale raw by %</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          {fieldLabel}
          <input
            className={inputClass}
            inputMode="decimal"
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
        </label>
        {active === "scale" && (
          <label className="flex flex-col gap-1 text-sm">
            Rounding
            <select
              className={inputClass}
              value={rounding}
              onChange={(event) => setRounding(event.target.value as RomRounding | "")}
            >
              <option value="">Choose rounding…</option>
              {ROUNDINGS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
        )}
        <button type="submit" className={buttonClass} disabled={!ready}>
          Apply to selection
        </button>
      </div>
      {active === "scale" && (
        <p className="text-xs text-fg-faint">
          Scales the stored raw counts, not the engineering value
          {table.decode?.kind === "reciprocal"
            ? `: ${table.decode.quantity} moves the opposite way`
            : ""}
          . Rounding has no default.
        </p>
      )}
      {refusal && (
        <p role="alert" className="text-sm text-bad">
          Refused ({refusal.reason}): {refusal.message}
        </p>
      )}
    </form>
  );
}

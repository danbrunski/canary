import { buttonClass, dangerButtonClass, inputClass } from "../../../styles";
import type { PendingEdit } from "../../../types/romEditor";

/** The edits applied so far, each removable, the label the spec carries,
 *  and the Preview button. Nothing is sent until Preview. */
export function PendingEdits({
  pending,
  label,
  onLabel,
  onRemove,
  onPreview,
  busy,
}: {
  pending: PendingEdit[];
  label: string;
  onLabel: (label: string) => void;
  onRemove: (id: string) => void;
  onPreview: () => void;
  busy: boolean;
}) {
  const blocked =
    pending.length === 0 ? "Apply at least one edit." : !label.trim() ? "Name the edit set." : null;
  return (
    <div className="space-y-3">
      {pending.length === 0 ? (
        <p className="text-sm text-fg-dim">No pending edits.</p>
      ) : (
        <ol aria-label="Pending edits" className="space-y-2">
          {pending.map((item, index) => (
            <li
              key={item.id}
              className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-border p-2 text-sm"
            >
              <span>
                <span className="font-semibold">
                  {index + 1}. {item.tableName}:
                </span>{" "}
                {item.summary}
                {item.chain && (
                  <span className="block font-num text-xs text-fg-dim">{item.chain}</span>
                )}
              </span>
              <button
                type="button"
                className={dangerButtonClass}
                aria-label={`Remove edit ${index + 1}`}
                onClick={() => onRemove(item.id)}
              >
                Remove
              </button>
            </li>
          ))}
        </ol>
      )}
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex min-w-60 flex-1 flex-col gap-1 text-sm">
          Label for this edit set
          <input
            className={inputClass}
            value={label}
            placeholder="idle 2 % richer"
            onChange={(event) => onLabel(event.target.value)}
          />
        </label>
        <button
          type="button"
          className={buttonClass}
          disabled={blocked !== null || busy}
          title={blocked ?? undefined}
          onClick={onPreview}
        >
          Preview
        </button>
      </div>
      {blocked && pending.length > 0 && <p className="text-xs text-fg-faint">{blocked}</p>}
    </div>
  );
}

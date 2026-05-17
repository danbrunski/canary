import type { ConnectionState } from "../types";
import { cn } from "../styles";

interface ConnectionIndicatorProps {
  label: string;
  state: ConnectionState;
}

/** Small dot + label per stream. Renders in the shell's header; renders
 *  the same way for all three states (connecting/open/closed/error) so a
 *  red dot is unambiguous. */
export function ConnectionIndicator({ label, state }: ConnectionIndicatorProps) {
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border border-border-soft bg-black/25 px-2.5 py-1 text-[0.66rem] font-semibold uppercase tracking-[0.04em] text-fg-dim"
      title={`${label}: ${state}`}
    >
      <span
        className={cn(
          "h-2 w-2 rounded-full bg-fg-faint transition-[background,box-shadow]",
          state === "open" && "bg-ok shadow-[0_0_0_3px_rgba(95,227,179,0.18)]",
          state === "connecting" &&
            "animate-pulse bg-warn shadow-[0_0_0_3px_rgba(255,209,102,0.18)]",
          (state === "error" || state === "closed") &&
            "bg-bad shadow-[0_0_0_3px_rgba(255,111,111,0.2)]",
        )}
      />
      {label}
    </span>
  );
}

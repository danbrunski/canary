import type { ConnectionState } from "../types";

interface ConnectionIndicatorProps {
  label: string;
  state: ConnectionState;
}

/** Small dot + label per stream. Renders in the shell's header; renders
 *  the same way for all three states (connecting/open/closed/error) so a
 *  red dot is unambiguous. */
export function ConnectionIndicator({ label, state }: ConnectionIndicatorProps) {
  return (
    <span className={`conn conn-${state}`} title={`${label}: ${state}`}>
      <span className="dot" />
      {label}
    </span>
  );
}

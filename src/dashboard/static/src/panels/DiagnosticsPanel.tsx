import { Panel } from "../components/Panel";
import { placeholderClass } from "../styles";
import type { AppMode } from "../types";

interface DiagnosticsPanelProps {
  mode: AppMode;
}

/** Parked-mode diagnostics (Task 5.14).
 *
 *  Stub. The UI surface for proprietary protocol reads and any future
 *  control actions lives here once they're implemented; for v1 the gate
 *  itself is the deliverable. Two safety properties:
 *
 *  1. **Driving mode hides this panel entirely.** Not "shows it
 *     disabled" — completely absent from the rendered tree, so a tap can't
 *     accidentally land here.
 *  2. **Read-only is the default.** When tools land here, separating
 *     read-only diagnostics from write/control is the design rule
 *     (`docs/dashboard-display-strategy.md`). Each control will need a
 *     confirmation prompt and the backend must enforce parked-mode too —
 *     the frontend gate is for the operator, not the security boundary.
 */
export function DiagnosticsPanel({ mode }: DiagnosticsPanelProps) {
  if (mode !== "parked") return null;
  return (
    <Panel
      title="Diagnostics"
      className="bg-accent/10"
      actions={
        <span className="text-[0.7rem] uppercase tracking-[0.1em] text-accent">
          parked / maintenance mode
        </span>
      }
      centerHeader
    >
      <p className={placeholderClass}>
        Diagnostic tools land here as they're built. Each control will
        require explicit confirmation and backend parked-mode enforcement.
      </p>
    </Panel>
  );
}

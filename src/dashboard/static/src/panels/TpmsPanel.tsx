import { Panel } from "../components/Panel";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { placeholderClass } from "../styles";

interface TpmsPanelProps {
  channels: Map<string, ChannelSnapshot>;
}

/** TPMS panel (Task 5.13).
 *
 *  Stub for now — `data/bindings.yaml` records four 0x385 pressure candidates,
 *  but they remain below dashboard-visible confidence until a controlled
 *  one-tire pressure change validates the Xterra scale and corner order.
 *  When promoted, this panel will pick them up automatically. Until then,
 *  surface the unvalidated state honestly so an operator doesn't mistake
 *  hypothesis-level values for measurements.
 */
const TPMS_CHANNELS = [
  "decoded:TPMS_FL_PRESSURE",
  "decoded:TPMS_FR_PRESSURE",
  "decoded:TPMS_RL_PRESSURE",
  "decoded:TPMS_RR_PRESSURE",
];

export function TpmsPanel({ channels }: TpmsPanelProps) {
  const found = TPMS_CHANNELS.map((key) => channels.get(key)).filter(Boolean);
  return (
    <Panel title="TPMS">
      {found.length === 0 ? (
        <p className={placeholderClass}>
          TPMS bindings hypothesized from CAN ID 0x385 but not yet validated —
          see <code>data/bindings.yaml</code>. Once the four per-tire pressure
          signals reach confidence ≥ medium, they'll appear here automatically.
        </p>
      ) : (
        <div className="grid grid-cols-2 gap-2">{/* future: per-corner cards */}</div>
      )}
    </Panel>
  );
}

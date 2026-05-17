import { Panel } from "../components/Panel";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { placeholderClass } from "../styles";

interface TpmsPanelProps {
  channels: Map<string, ChannelSnapshot>;
}

/** TPMS panel (Task 5.13).
 *
 *  Stub for now — the Xterra's TPMS signals haven't been decoded in
 *  `data/bindings.yaml` yet, so the channel keys this panel would render
 *  (decoded:TPMS_FL_PRESSURE etc.) don't exist on the wire. When they do,
 *  this panel will pick them up automatically — the rendering is keyed off
 *  the decoded channel set, so no change is needed here beyond filling in
 *  the TPMS_CHANNELS list. Until then, surface the "not decoded" state
 *  honestly so an operator doesn't think the tires are reporting fine
 *  when in reality the dashboard has no data.
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
          TPMS bindings not yet pinned — see <code>data/bindings.yaml</code>.
          Once the four per-tire pressure signals are decoded with
          confidence ≥ medium, they'll appear here automatically.
        </p>
      ) : (
        <div className="grid grid-cols-2 gap-2">{/* future: per-corner cards */}</div>
      )}
    </Panel>
  );
}

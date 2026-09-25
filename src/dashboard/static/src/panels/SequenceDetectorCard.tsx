import { switchSequenceDetector } from "../api/http";
import { Panel } from "../components/Panel";
import type { RunAction } from "../hooks/useAsyncAction";
import { buttonClass, cn } from "../styles";
import type { SequenceDetectorStatus } from "../types";

interface SequenceDetectorCardProps {
  token: string;
  busy: string | null;
  run: RunAction;
  sequenceDetector: SequenceDetectorStatus | null;
}

/** LSTM/TCN backbone switcher, extracted from ControlPanel's god-panel
 *  (Task 3.2). Stateless — `run`'s own post-action refresh (the parent's
 *  `refreshControl`) is what brings `sequenceDetector` up to date after a
 *  switch. */
export function SequenceDetectorCard({
  token,
  busy,
  run,
  sequenceDetector,
}: SequenceDetectorCardProps) {
  const switchBackbone = (backbone: "lstm" | "tcn") =>
    run(`sequence switch → ${backbone}`, async () => {
      await switchSequenceDetector(token, backbone);
    });

  return (
    <Panel
      title="Sequence Detector"
      actions={<span className="text-[0.8rem] text-fg-dim">{sequenceDetector?.active ?? "—"}</span>}
    >
      <p className="mb-2 text-[0.75rem] text-fg-dim">
        Live backbone for the sequence slot. A switch takes effect immediately and is not persisted
        — set the durable default via <code>models.sequence_detector</code>.
      </p>
      <div className="grid grid-cols-2 gap-2 max-[1100px]:grid-cols-1">
        {(["lstm", "tcn"] as const).map((backbone) => {
          const info = sequenceDetector?.available[`${backbone}_autoencoder`];
          const isActive = info?.active ?? false;
          return (
            <button
              key={backbone}
              type="button"
              className={cn(buttonClass, isActive && "border-accent")}
              onClick={() => switchBackbone(backbone)}
              disabled={!token || !!busy || isActive}
              title={
                info?.artifact_present
                  ? `artifact: ${info.version ?? "present"}`
                  : "no trained artifact yet"
              }
            >
              {backbone.toUpperCase()}
              {isActive ? " (active)" : ""}
              {info && !info.artifact_present ? " · no artifact" : ""}
            </button>
          );
        })}
      </div>
    </Panel>
  );
}

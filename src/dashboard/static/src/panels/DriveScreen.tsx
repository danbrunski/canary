import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { cn, responsiveGridClass, scrollPaneClass } from "../styles";
import type { BindingsByChannel } from "../types";
import { TelemetryPanel } from "./TelemetryPanel";

interface DriveScreenProps {
  channels: Map<string, ChannelSnapshot>;
  bindings: BindingsByChannel;
}

/** The Drive context: one locked, glanceable screen. The compact gauge
 *  cluster and nothing else — the "is everything OK" facts (anomaly state,
 *  freshness, model health) live in the always-visible StatusStrip above,
 *  so a moving driver never navigates and never hunts for status. The full
 *  workbench is one tap away via the header mode toggle. */
export function DriveScreen({ channels, bindings }: DriveScreenProps) {
  return (
    <div className={scrollPaneClass}>
      <div className={cn(responsiveGridClass, "grid-cols-1")}>
        <TelemetryPanel channels={channels} bindings={bindings} compact />
      </div>
    </div>
  );
}

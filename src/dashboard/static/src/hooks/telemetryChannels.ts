import { createContext, useContext } from "react";

import type { ChannelSnapshot } from "./useTelemetryStream";

/** The live channels, for a component that sits below a `memo` boundary.
 *
 *  Telemetry replaces the channel Map up to once per animation frame. A prop
 *  carrying it re-renders every component between the owner and the reader, so
 *  a page that only needs it deep in its tree passes it through context
 *  instead: a memoised subtree that never reads the context is skipped, and
 *  only the reader renders. The default is an empty Map: no provider, no data. */
export const TelemetryChannelsContext = createContext<Map<string, ChannelSnapshot>>(new Map());

export function useTelemetryChannels(): Map<string, ChannelSnapshot> {
  return useContext(TelemetryChannelsContext);
}

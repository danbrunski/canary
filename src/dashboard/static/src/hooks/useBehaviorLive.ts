import { getBehaviorLive } from "../api/http";
import { usePolling } from "./usePolling";

/** Poll `/models/behavior/live` at ``intervalMs`` (default 2 s).
 *
 *  Deliberately decoupled from `useBehaviorModels` (60 s): the static
 *  surfaces only change on a manual refit, but the operating point moves
 *  every inference tick. The rolling trail lives server-side, so even this
 *  2 s cadence yields a smooth trajectory rather than the sparse points a
 *  client-side sampler would collect. Errors are swallowed — the overlay is
 *  additive, the static surfaces must keep rendering if the daemon is down
 *  (`usePolling` keeps the last good data across a failure). */
export function useBehaviorLive(intervalMs = 2_000) {
  return usePolling(getBehaviorLive, { intervalMs }).data;
}

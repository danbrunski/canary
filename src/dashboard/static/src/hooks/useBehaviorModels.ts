import { getBehaviorModels } from "../api/http";
import { usePolling } from "./usePolling";

/** Poll `/models/behavior` at ``intervalMs`` (default 60 s).
 *
 *  Behavior models change on manual `refit_behavior_models.py` runs, so a
 *  minute cadence is plenty — the alternative would be a WebSocket
 *  notification, but the data is small enough that polling stays free. */
export function useBehaviorModels(intervalMs = 60_000) {
  const { data, error } = usePolling(getBehaviorModels, { intervalMs });
  return { data, error: error instanceof Error ? error : null };
}

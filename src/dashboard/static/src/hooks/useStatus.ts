import { getStatus } from "../api/http";
import { usePolling } from "./usePolling";

/** Polls `/status` every ``intervalMs`` (default 5 s) after the previous poll
 *  settles. Returns the latest snapshot, the load error (if any), and a
 *  `loading` flag for the first request. */
export function useStatus(intervalMs = 5000) {
  const { data, error, loading } = usePolling(getStatus, { intervalMs });
  return { status: data, error: error instanceof Error ? error : null, loading };
}

// Shared data-freshness helper. Both the StatusStrip (Phase A) and
// SystemPanel (migrated in Phase B) answer "how stale is the newest sample
// across all channels?" — keep that one definition in one place.

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";

/** Milliseconds since the newest timestamp seen across all channels, or
 *  `null` when no channel has a parseable timestamp yet. */
export function computeFreshness(
  channels: Map<string, ChannelSnapshot>,
  now: number = Date.now(),
): number | null {
  let newest: number | null = null;
  for (const snap of channels.values()) {
    const ts = snap.latest.timestamp;
    if (ts == null) continue;
    const t = Date.parse(ts);
    if (Number.isNaN(t)) continue;
    if (newest === null || t > newest) newest = t;
  }
  return newest === null ? null : now - newest;
}

/** Newest sample older than this (ms) ⇒ surface a "stale" warning. */
export const STALE_THRESHOLD_MS = 10_000;

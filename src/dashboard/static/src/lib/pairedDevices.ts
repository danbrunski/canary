// Display rules for paired devices (docs/roadmap/archive/phone-ui.md P2.4).

import type { PairedDevice } from "../api/pairedDevices";
import type { ControlEvent } from "../types";

const APPROVED_BY = "approved-by:";

/** How a device was paired, in words. An approver that is itself a paired
 *  device is named by its current name when it is still in the list. */
export function methodLabel(method: string, devices: readonly PairedDevice[]): string {
  if (method === "key-cycle") return "key cycle";
  if (!method.startsWith(APPROVED_BY)) return method;
  const by = method.slice(APPROVED_BY.length);
  if (by === "shared-token") return "approved with the shared token";
  if (by === "loopback") return "approved on the Pi itself";
  const approver = devices.find((d) => d.id === by);
  return `approved by ${approver ? approver.name : `a removed device (${by})`}`;
}

/** A Unix-seconds stamp as local date and time; `never` when missing. */
export function formatWhen(unixSeconds: number | null): string {
  if (unixSeconds === null) return "never";
  return new Date(unixSeconds * 1000).toLocaleString([], {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

/** Events that change who is waiting to pair: requested, approved, paired,
 *  a conflict. Any of them means the waiting list should be re-read. */
export function isPairingEvent(event: ControlEvent): boolean {
  return typeof event.action === "string" && event.action.startsWith("pairing.");
}

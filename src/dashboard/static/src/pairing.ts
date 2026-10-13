// Pairing this device by key cycle (docs/roadmap/phone-ui.md P2.4).
//
// A device without a control token asks to pair, then the person in the car
// turns the key ACC -> ON -> ACC. The Pi sees the ECM wake and sleep and
// approves the request, which hands this device its own token. Nothing
// secret is typed, pasted, or sent over Bluetooth.

import { HttpError } from "./api/core";
import type { PairingRequestCreated, PairingRequestStatus, PairingStep } from "./api/http";

/** The request in progress, so a reload resumes it instead of opening a
 *  second one, which the Pi would treat as two devices asking at once. */
export const ACTIVE_REQUEST_KEY = "canary.pairingRequest";

/** A readable name for the paired-devices list, from the user agent. */
export function deviceName(userAgent: string): string {
  if (/Android/i.test(userAgent)) {
    return /Mobile/i.test(userAgent) ? "Android phone" : "Android tablet";
  }
  if (/iPhone/.test(userAgent)) return "iPhone";
  if (/iPad/.test(userAgent)) return "iPad";
  if (/Windows/.test(userAgent)) return "Windows computer";
  if (/Macintosh/.test(userAgent)) return "Mac";
  if (/Linux/.test(userAgent)) return "Linux computer";
  return "Browser";
}

export interface KeyStep {
  label: string;
  done: boolean;
  current: boolean;
}

/** The three key positions, ticked off as the Pi sees them. */
export function keySteps(step: PairingStep | undefined): KeyStep[] {
  const reached = step === "turn_on" ? 1 : step === "back_to_acc" ? 2 : 0;
  const labels = ["Key to ACC", "Key to ON", "Back to ACC"];
  return labels.map((label, i) => ({
    label,
    done: i < reached,
    current: i === reached && step !== "unavailable",
  }));
}

/** What to tell the person in the car right now. */
export function instruction(status: PairingRequestStatus): string {
  switch (status.step) {
    case "set_acc":
      return (
        "Turn the key to ACC. Not OFF: if the Pi runs from a switched socket, " +
        "OFF would power it down."
      );
    case "turn_on":
      return "Turn the key to ON. You do not need to start the engine.";
    case "back_to_acc":
      return "Now turn it back to ACC. The Pi can take up to 10 seconds to notice.";
    case "unavailable":
      return status.detail || "The Pi cannot see the key right now.";
    default:
      return status.detail;
  }
}

export function failureMessage(error: unknown): string {
  if (error instanceof HttpError) {
    try {
      const detail = (JSON.parse(error.body) as { detail?: unknown }).detail;
      if (typeof detail === "string") return detail;
    } catch {
      /* not JSON — fall through */
    }
    return `Pairing failed (HTTP ${error.status}).`;
  }
  return "The Pi did not answer. Check this device is on the same WiFi network as the Pi.";
}

export function loadActiveRequest(): PairingRequestCreated | null {
  try {
    const raw = window.sessionStorage.getItem(ACTIVE_REQUEST_KEY);
    if (raw === null) return null;
    const parsed = JSON.parse(raw) as Partial<PairingRequestCreated>;
    return typeof parsed.id === "string" && typeof parsed.secret === "string"
      ? { id: parsed.id, secret: parsed.secret }
      : null;
  } catch {
    return null;
  }
}

export function saveActiveRequest(request: PairingRequestCreated | null): void {
  try {
    if (request === null) window.sessionStorage.removeItem(ACTIVE_REQUEST_KEY);
    else window.sessionStorage.setItem(ACTIVE_REQUEST_KEY, JSON.stringify(request));
  } catch {
    /* storage unavailable: a reload then starts a fresh request */
  }
}

// Workbench helpers that are not API calls (R08.4): the structured-refusal
// reader and the gate sentence. Routes are in api/workbench.ts, types in
// types/workbench.ts.

import { HttpError } from "../api/core";
import { describeError } from "../hooks/useControlResource";
import type { GateReport, Refusal, Regime } from "../types/workbench";

export const REGIMES: ReadonlyArray<{ id: Regime; label: string }> = [
  { id: "all", label: "All" },
  { id: "closed_loop", label: "Closed loop" },
  { id: "enrichment", label: "Enrichment" },
  { id: "fuel_cut", label: "Fuel cut" },
  { id: "idle", label: "Idle" },
];

export function describeRefusal(error: unknown): Refusal {
  // Anything unstructured reads like every other page: a dead Pi is "Can't
  // reach the daemon…", a 401 is "Not authorized…" (R08.2).
  if (!(error instanceof HttpError)) {
    return { kind: "error", message: describeError(error) };
  }
  let detail: unknown;
  try {
    detail = (JSON.parse(error.body) as { detail?: unknown }).detail;
  } catch {
    detail = null;
  }
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    const message = typeof d.message === "string" ? d.message : describeError(error);
    switch (d.error) {
      case "analysis_deferred":
        return {
          kind: "deferred",
          message,
          profile: typeof d.profile === "string" ? d.profile : undefined,
          laptopPath: typeof d.laptop_path === "string" ? d.laptop_path : undefined,
        };
      case "analysis_busy":
        return { kind: "busy", message };
      case "analysis_unavailable":
        return { kind: "unavailable", message };
      case "no_sessions":
        return { kind: "no_sessions", message };
      case "session_not_found":
        return { kind: "not_found", message };
      default:
        return { kind: "refused", message };
    }
  }
  if (typeof detail === "string") {
    return { kind: error.status === 404 ? "not_found" : "refused", message: detail };
  }
  return { kind: "error", message: describeError(error) };
}

/** "0 of N samples passed: <gate> removed M, …" — the gates-removed-everything
 *  sentence, from a gate report. */
export function gateSentence(gates: GateReport | null | undefined): string {
  if (!gates) return "no gate report";
  const removed = Object.entries(gates.filtered)
    .filter(([, count]) => count > 0)
    .map(([gate, count]) => `${gate.replace(/_/g, " ")} removed ${count}`);
  return `${gates.remaining} of ${gates.started} samples passed${removed.length ? `: ${removed.join(", ")}` : ""}`;
}

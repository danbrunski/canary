import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { BindingDisplay } from "../types";
import { formatNumber } from "../util/format";

export interface RenderedValue {
  text: string;
  unit: string;
  bandClass: "" | "band-warn" | "band-bad";
  /** True when this is the raw decoded integer, not an engineering value:
   * a decoded CAN channel whose binding has no pinned scale. */
  raw: boolean;
}

/** Apply binding scale/offset/unit, decide whether to color the card. */
export function renderChannel(
  snapshot: ChannelSnapshot,
  binding: BindingDisplay | undefined,
): RenderedValue {
  const raw = snapshot.latest.value;
  if (raw === null || raw === undefined) {
    return { text: "—", unit: "", bandClass: "", raw: false };
  }
  if (typeof raw === "string" || typeof raw === "boolean") {
    return { text: String(raw), unit: cleanUnit(snapshot.latest.unit), bandClass: "", raw: false };
  }
  if (!Number.isFinite(raw)) {
    return { text: "—", unit: "", bandClass: "", raw: false };
  }

  const isDecoded = snapshot.latest.source === "decoded";
  let value = raw;
  if (binding && binding.scale !== null && isDecoded) {
    value = raw * binding.scale + (binding.offset ?? 0);
  }

  const rawDisplay = isDecoded && (!binding || binding.scale === null);
  const unit = cleanUnit(binding?.unit) || cleanUnit(snapshot.latest.unit);
  const text = formatNumber(value);

  let bandClass: RenderedValue["bandClass"] = "";
  if (binding) {
    const lo = binding.plausible_min;
    const hi = binding.plausible_max;
    if ((lo !== null && value < lo) || (hi !== null && value > hi)) {
      bandClass = "band-bad";
    } else if (lo !== null && hi !== null) {
      const span = hi - lo;
      const margin = span * 0.05;
      if (value < lo + margin || value > hi - margin) bandClass = "band-warn";
    }
  }

  return { text, unit, bandClass, raw: rawDisplay };
}

/** `unknown` is a placeholder in bindings.yaml for a not-yet-RE'd unit. */
function cleanUnit(unit: string | null | undefined): string {
  if (!unit || unit === "unknown") return "";
  return unit;
}

export function sparkSeries(
  snapshot: ChannelSnapshot,
  binding: BindingDisplay | undefined,
): number[] {
  if (!binding || binding.scale === null || snapshot.latest.source !== "decoded") {
    return snapshot.history;
  }
  const offset = binding.offset ?? 0;
  return snapshot.history.map((raw) => raw * binding.scale! + offset);
}

/** Drop the source prefix so the card reads `RPM` instead of `obd:RPM`. */
export function prettyChannel(key: string): string {
  const idx = key.indexOf(":");
  if (idx === -1) return key;
  return key.slice(idx + 1);
}

export function isEmptyRawCan(snapshot: ChannelSnapshot): boolean {
  return snapshot.latest.source === "can" && snapshot.latest.value === null;
}

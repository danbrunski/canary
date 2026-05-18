// Pure model-evidence shaping: parse the untyped anomaly `detail` blob into
// the channel/residual/heatmap/overlay structures the evidence cards
// render. Split out of ModelEvidencePanel so this (the testable part) has
// no React dependency and the panel is just presentation.

import type { AnomalyEvent } from "../types";
import { numberArray, numberMap, numberOrNull, numberSeriesMap, record } from "../util/json";

export type EvidenceSource = "isolation_forest" | "lstm_autoencoder";

export const SOURCES: EvidenceSource[] = ["isolation_forest", "lstm_autoencoder"];

export const SOURCE_LABEL: Record<EvidenceSource, string> = {
  isolation_forest: "Isolation Forest",
  lstm_autoencoder: "LSTM",
};

export interface ChannelEvidence {
  channel: string;
  score: number;
  threshold: number | null;
  ratio: number | null;
}

export interface ResidualEvidence {
  name: string;
  severity: string | null;
  actual: number | null;
  expected: number | null;
  deviation: number | null;
}

export interface HeatmapData {
  channels: string[];
  values: number[][];
  max: number;
}

export interface OverlaySeries {
  channel: string;
  actual: number[];
  reconstructed: number[];
}

export function extractChannelEvidence(event: AnomalyEvent): ChannelEvidence[] {
  const detail = event.detail;
  const scores =
    numberMap(detail.channel_scores) ??
    numberMap(detail.feature_contributors) ??
    numberMap(detail.contributors);
  if (!scores) {
    return event.affected_channels.map((channel) => ({
      channel,
      score: 1,
      threshold: null,
      ratio: null,
    }));
  }
  const thresholds = numberMap(detail.channel_thresholds) ?? {};
  return Object.entries(scores)
    .map(([channel, score]) => {
      const threshold = thresholds[channel] ?? null;
      const ratio = threshold !== null && threshold > 0 ? score / threshold : null;
      return { channel, score, threshold, ratio };
    })
    .sort((a, b) => {
      const av = a.ratio ?? a.score;
      const bv = b.ratio ?? b.score;
      return bv - av;
    });
}

export function extractHeatmap(
  detail: Record<string, unknown>,
  fallback: ChannelEvidence[],
): HeatmapData {
  const trace =
    numberSeriesMap(detail.channel_score_series) ??
    numberSeriesMap(detail.reconstruction_series) ??
    numberSeriesMap(detail.reconstruction_trace);
  if (trace) {
    const channels = Object.keys(trace).sort((a, b) => maxOf(trace[b]) - maxOf(trace[a]));
    const values = channels.map((channel) => trace[channel]);
    return { channels, values, max: Math.max(1, ...values.flat()) };
  }

  const rows = fallback.map((channel) => {
    const value = channel.ratio ?? channel.score;
    return { channel: channel.channel, value };
  });
  return {
    channels: rows.map((row) => row.channel),
    values: rows.map((row) => [row.value]),
    max: Math.max(1, ...rows.map((row) => row.value)),
  };
}

export function extractOverlays(
  detail: Record<string, unknown>,
  preferredChannels: string[],
): OverlaySeries[] {
  const raw = record(detail.reconstruction_overlays) ?? record(detail.reconstruction_overlay);
  if (!raw) return [];
  const out: OverlaySeries[] = [];
  for (const channel of preferredChannels) {
    const item = record(raw[channel]);
    if (!item) continue;
    const actual = numberArray(item.actual);
    const reconstructed = numberArray(item.reconstructed ?? item.reconstruction);
    if (actual.length > 1 && reconstructed.length === actual.length) {
      out.push({ channel, actual, reconstructed });
    }
  }
  return out;
}

export function extractResiduals(detail: Record<string, unknown>): ResidualEvidence[] {
  const candidates = [
    detail.behavior_residuals,
    detail.physics_residuals,
    detail.residuals,
  ];
  const out: ResidualEvidence[] = [];
  for (const candidate of candidates) {
    if (!Array.isArray(candidate)) continue;
    for (const item of candidate) {
      const obj = record(item);
      if (!obj) continue;
      out.push({
        name: String(obj.name ?? obj.channel ?? obj.model ?? "residual"),
        severity: typeof obj.severity === "string" ? obj.severity : null,
        actual: numberOrNull(obj.actual),
        expected: numberOrNull(obj.expected),
        deviation: numberOrNull(obj.deviation ?? obj.residual),
      });
    }
  }
  return out;
}

function relatedTime(event: AnomalyEvent): number | null {
  const t = Date.parse(event.window_end_timestamp ?? event.timestamp);
  return Number.isNaN(t) ? null : t;
}

/** Events within 5 s of `event` (same session when both name one) — the
 *  cross-model agreement set the evidence panel correlates. */
export function relatedEventsFor(
  event: AnomalyEvent | null,
  events: AnomalyEvent[],
): AnomalyEvent[] {
  if (!event) return [];
  const anchor = relatedTime(event);
  return events.filter((candidate) => {
    if (candidate === event) return true;
    if (event.session && candidate.session && event.session !== candidate.session) return false;
    const t = relatedTime(candidate);
    if (anchor === null || t === null) return false;
    return Math.abs(t - anchor) <= 5_000;
  });
}

export function maxOf(values: number[]): number {
  return Math.max(0, ...values);
}

/** Lerp a value→max into the dark→hot heatmap ramp. */
export function heatColor(value: number, max: number): string {
  const t = Math.max(0, Math.min(1, value / (max || 1)));
  const r = Math.round(31 + (255 - 31) * t);
  const g = Math.round(38 + (111 - 38) * t);
  const b = Math.round(45 + (111 - 45) * t);
  return `rgb(${r}, ${g}, ${b})`;
}

export function severityTone(severity: string): string {
  if (severity === "critical") return "text-bad";
  if (severity === "warning") return "text-warn";
  return "text-ok";
}

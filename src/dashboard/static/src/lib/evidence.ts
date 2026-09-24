// Pure model-evidence shaping: parse the untyped anomaly `detail` blob into
// the channel/residual/heatmap/overlay structures the evidence cards
// render. Split out of ModelEvidencePanel so this (the testable part) has
// no React dependency and the panel is just presentation.

import type { AnomalyEvent } from "../types";
import { numberArray, numberMap, numberOrNull, numberSeriesMap, record } from "../util/json";

export type EvidenceSource = "isolation_forest" | "lstm_autoencoder";

export const SOURCES: EvidenceSource[] = ["isolation_forest", "lstm_autoencoder"];

/** Narrow an `AnomalyEvent.source` (which now also includes the Phase 7.2
 *  `fused` source) to a per-detector `EvidenceSource`. The evidence cards are
 *  per-detector; a fused event is shown via its `detail.sources` breakdown,
 *  not as its own card here (7.2a enriches this). */
export function isEvidenceSource(source: string): source is EvidenceSource {
  return (SOURCES as readonly string[]).includes(source);
}

export const SOURCE_LABEL: Record<EvidenceSource, string> = {
  isolation_forest: "Isolation Forest",
  lstm_autoencoder: "LSTM",
};

/** A03.7 — what a sequence-detector channel ranking is. It ranks each
 *  channel's reconstruction error against its own calibrated band over every
 *  column, driving conditions included (it named barometric pressure on every
 *  altitude-trip event), so it is the largest deviation from the training
 *  envelope — never presented as the event's cause. */
export const SEQUENCE_RANKING_CAPTION = "largest deviation from training envelope";
/** Fallback title for events recorded before the API carried
 *  `detail.channel_ranking_note`; every artifact of that era ranked all
 *  channels, driving conditions included. The note, when present, says which
 *  scope this artifact ranks (engine-response artifacts never rank context). */
export const SEQUENCE_RANKING_TITLE =
  "Channels whose reconstruction error sits furthest above their calibrated band, " +
  "driving conditions (baro, speed, intake temperature, ...) included. This says where " +
  "the window is furthest from what the model trained on, not what caused the event.";

export function channelRankingCaption(
  event: AnomalyEvent,
): { caption: string; title: string } | null {
  const ranking = event.detail?.channel_ranking;
  if (
    event.source === "lstm_autoencoder" ||
    ranking === "largest_deviation_from_training_envelope"
  ) {
    const note = event.detail?.channel_ranking_note;
    return {
      caption: SEQUENCE_RANKING_CAPTION,
      title: typeof note === "string" && note ? note : SEQUENCE_RANKING_TITLE,
    };
  }
  return null;
}

/** A06.2 — the sequence detector's novelty screen, as the event carries it:
 *  `detail.novelty_summary` (the sentence) and `detail.envelope` (the
 *  structured verdict, `null` when the artifact recorded no envelope).
 *  Events stored before A06.2 carry neither and return `null`. */
export interface NoveltyScreen {
  sentence: string;
  label: string;
  outside: boolean;
}

export function noveltyScreen(event: AnomalyEvent): NoveltyScreen | null {
  const sentence = event.detail?.novelty_summary;
  if (typeof sentence !== "string" || !sentence) return null;
  const envelope = record(event.detail?.envelope);
  if (envelope === null) {
    return { sentence, label: "envelope not recorded", outside: false };
  }
  const status = envelope.status;
  if (status === "outside" || status === "mode_not_trained") {
    return { sentence, label: "outside envelope", outside: true };
  }
  if (status === "inside") {
    return { sentence, label: "inside envelope", outside: false };
  }
  return { sentence, label: "envelope not compared", outside: false };
}

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

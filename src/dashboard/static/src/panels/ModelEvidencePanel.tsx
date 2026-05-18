import type { AnomalyEvent } from "../types";
import { badgeClass, cn, placeholderClass } from "../styles";

interface ModelEvidencePanelProps {
  event: AnomalyEvent | null;
  relatedEvents: AnomalyEvent[];
}

type EvidenceSource = "isolation_forest" | "lstm_autoencoder";

const SOURCES: EvidenceSource[] = ["isolation_forest", "lstm_autoencoder"];

const SOURCE_LABEL: Record<EvidenceSource, string> = {
  isolation_forest: "Isolation Forest",
  lstm_autoencoder: "LSTM",
};

interface ChannelEvidence {
  channel: string;
  score: number;
  threshold: number | null;
  ratio: number | null;
}

interface ResidualEvidence {
  name: string;
  severity: string | null;
  actual: number | null;
  expected: number | null;
  deviation: number | null;
}

export function ModelEvidencePanel({
  event,
  relatedEvents,
}: ModelEvidencePanelProps) {
  if (!event) return null;

  const sourceEvents = new Map<EvidenceSource, AnomalyEvent>();
  for (const source of SOURCES) {
    const match = relatedEvents.find((e) => e.source === source);
    if (match) sourceEvents.set(source, match);
  }
  if (SOURCES.includes(event.source)) {
    sourceEvents.set(event.source, event);
  }

  const residuals = extractResiduals(event.detail);
  const agreement = [
    sourceEvents.has("isolation_forest") ? "IF" : null,
    sourceEvents.has("lstm_autoencoder") ? "LSTM" : null,
    residuals.length > 0 ? "residual" : null,
  ].filter(Boolean);

  return (
    <section className="mb-3 rounded-[8px] border border-border-soft bg-black/20 p-3">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 className="m-0 text-[0.78rem] font-bold uppercase tracking-[0.12em] text-fg-dim">
            Model evidence
          </h3>
          <div className="mt-1 flex flex-wrap gap-2 text-[0.72rem] text-fg-faint">
            <span>{formatTimestamp(event.timestamp)}</span>
            {event.operating_mode && <span>{event.operating_mode}</span>}
            {event.window_start_timestamp && event.window_end_timestamp && (
              <span>
                {formatTimestamp(event.window_start_timestamp)}-
                {formatTimestamp(event.window_end_timestamp)}
              </span>
            )}
          </div>
        </div>
        <span
          className={cn(
            badgeClass,
            agreement.length > 1
              ? "border-ok/60 text-ok"
              : "border-warn/60 text-warn",
          )}
        >
          {agreement.length > 0 ? agreement.join(" + ") : "single source"}
        </span>
      </div>

      <div className="grid grid-cols-3 gap-2 max-[1200px]:grid-cols-1">
        {SOURCES.map((source) => (
          <SourceEvidenceCard
            key={source}
            source={source}
            event={sourceEvents.get(source) ?? null}
          />
        ))}
        <ResidualEvidenceCard residuals={residuals} />
      </div>
    </section>
  );
}

function SourceEvidenceCard({
  source,
  event,
}: {
  source: EvidenceSource;
  event: AnomalyEvent | null;
}) {
  const channelEvidence = event ? extractChannelEvidence(event) : [];
  return (
    <div className="min-w-0 rounded-[8px] border border-border-soft bg-elev/70 p-2.5">
      <div className="mb-2 flex items-center justify-between gap-2">
        <b className="text-[0.78rem] uppercase tracking-[0.08em] text-fg">
          {SOURCE_LABEL[source]}
        </b>
        <span
          className={cn(
            "font-num text-[0.68rem] uppercase tracking-[0.08em]",
            event ? "text-ok" : "text-fg-faint",
          )}
        >
          {event ? "fired" : "quiet"}
        </span>
      </div>
      {!event ? (
        <p className={placeholderClass}>no matching event in this window.</p>
      ) : (
        <>
          <dl className="m-0 grid grid-cols-2 gap-x-3 gap-y-1 text-[0.74rem]">
            <dt className="text-fg-faint">score</dt>
            <dd className="m-0 text-right font-num tabular-nums">
              {formatNumber(event.score)}
            </dd>
            <dt className="text-fg-faint">threshold</dt>
            <dd className="m-0 text-right font-num tabular-nums">
              {event.threshold === null ? "-" : formatNumber(event.threshold)}
            </dd>
            <dt className="text-fg-faint">latency</dt>
            <dd className="m-0 text-right font-num tabular-nums">
              {event.latency_ms === null ? "-" : `${event.latency_ms.toFixed(1)} ms`}
            </dd>
            <dt className="text-fg-faint">version</dt>
            <dd className="m-0 truncate text-right font-num" title={event.model_version}>
              {event.model_version}
            </dd>
          </dl>
          {event.affected_channels.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {event.affected_channels.slice(0, 6).map((channel) => (
                <span key={channel} className={cn(badgeClass, "normal-case tracking-normal")}>
                  {channel}
                </span>
              ))}
            </div>
          )}
          {source === "lstm_autoencoder" && channelEvidence.length > 0 && (
            <LstmReconstructionView detail={event.detail} channels={channelEvidence} />
          )}
          {source === "isolation_forest" && channelEvidence.length > 0 && (
            <ChannelBars channels={channelEvidence.slice(0, 8)} />
          )}
        </>
      )}
    </div>
  );
}

function ResidualEvidenceCard({ residuals }: { residuals: ResidualEvidence[] }) {
  return (
    <div className="min-w-0 rounded-[8px] border border-border-soft bg-elev/70 p-2.5">
      <div className="mb-2 flex items-center justify-between gap-2">
        <b className="text-[0.78rem] uppercase tracking-[0.08em] text-fg">
          Residuals
        </b>
        <span
          className={cn(
            "font-num text-[0.68rem] uppercase tracking-[0.08em]",
            residuals.length > 0 ? "text-warn" : "text-fg-faint",
          )}
        >
          {residuals.length > 0 ? "present" : "none"}
        </span>
      </div>
      {residuals.length === 0 ? (
        <p className={placeholderClass}>no residual evidence attached.</p>
      ) : (
        <div className="flex flex-col gap-1.5">
          {residuals.slice(0, 6).map((r) => (
            <div key={r.name} className="rounded-[6px] bg-black/20 p-1.5 text-[0.73rem]">
              <div className="flex items-baseline justify-between gap-2">
                <b className="truncate text-fg" title={r.name}>
                  {r.name}
                </b>
                {r.severity && (
                  <span className={cn("font-num uppercase", severityTone(r.severity))}>
                    {r.severity}
                  </span>
                )}
              </div>
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-fg-dim">
                {r.actual !== null && <span>actual {formatNumber(r.actual)}</span>}
                {r.expected !== null && <span>expected {formatNumber(r.expected)}</span>}
                {r.deviation !== null && <span>dev {formatNumber(r.deviation)}</span>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function LstmReconstructionView({
  detail,
  channels,
}: {
  detail: Record<string, unknown>;
  channels: ChannelEvidence[];
}) {
  const top = channels.slice(0, 8);
  const heatmap = extractHeatmap(detail, top);
  const overlays = extractOverlays(detail, top.map((c) => c.channel)).slice(0, 3);
  return (
    <div className="mt-3 flex flex-col gap-2">
      <ChannelBars channels={top} />
      <ChannelHeatmap heatmap={heatmap} />
      {overlays.length > 0 && (
        <div className="grid grid-cols-3 gap-1.5 max-[1200px]:grid-cols-1">
          {overlays.map((overlay) => (
            <OverlayChart key={overlay.channel} overlay={overlay} />
          ))}
        </div>
      )}
    </div>
  );
}

function ChannelBars({ channels }: { channels: ChannelEvidence[] }) {
  const max = Math.max(
    1,
    ...channels.map((c) => (c.ratio === null ? c.score : c.ratio)),
  );
  return (
    <div className="flex flex-col gap-1.5">
      {channels.map((c) => {
        const value = c.ratio === null ? c.score : c.ratio;
        const width = `${Math.max(3, Math.min(100, (value / max) * 100))}%`;
        return (
          <div key={c.channel} className="grid grid-cols-[minmax(5rem,1fr)_5rem] gap-2">
            <div className="min-w-0">
              <div className="mb-0.5 truncate text-[0.68rem] text-fg-dim" title={c.channel}>
                {c.channel}
              </div>
              <div className="h-2 rounded-full bg-black/30">
                <div
                  className={cn(
                    "h-2 rounded-full",
                    value >= 1 ? "bg-bad" : value >= 0.75 ? "bg-warn" : "bg-accent-2",
                  )}
                  style={{ width }}
                />
              </div>
            </div>
            <div className="self-end text-right font-num text-[0.68rem] tabular-nums text-fg-dim">
              {c.ratio === null ? formatNumber(c.score) : `${c.ratio.toFixed(2)}x`}
            </div>
          </div>
        );
      })}
    </div>
  );
}

interface HeatmapData {
  channels: string[];
  values: number[][];
  max: number;
}

function ChannelHeatmap({ heatmap }: { heatmap: HeatmapData }) {
  if (heatmap.channels.length === 0) return null;
  const cols = Math.max(1, heatmap.values[0]?.length ?? 1);
  return (
    <div className="grid grid-cols-[minmax(5rem,1fr)_minmax(7rem,2fr)] gap-2">
      <div className="flex flex-col gap-0.5">
        {heatmap.channels.map((channel) => (
          <div key={channel} className="truncate text-[0.62rem] leading-4 text-fg-faint" title={channel}>
            {channel}
          </div>
        ))}
      </div>
      <div
        className="grid gap-0.5"
        style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
      >
        {heatmap.values.flatMap((row, rowIndex) =>
          row.map((value, colIndex) => (
            <div
              key={`${rowIndex}-${colIndex}`}
              className="h-4 min-w-2 rounded-[3px]"
              style={{ backgroundColor: heatColor(value, heatmap.max) }}
              title={`${heatmap.channels[rowIndex]} ${formatNumber(value)}`}
            />
          )),
        )}
      </div>
    </div>
  );
}

interface OverlaySeries {
  channel: string;
  actual: number[];
  reconstructed: number[];
}

function OverlayChart({ overlay }: { overlay: OverlaySeries }) {
  const values = [...overlay.actual, ...overlay.reconstructed].filter(Number.isFinite);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const width = 160;
  const height = 44;
  const path = (series: number[]) =>
    series
      .map((value, index) => {
        const x = series.length <= 1 ? 0 : (index / (series.length - 1)) * width;
        const y = height - ((value - min) / span) * (height - 4) - 2;
        return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");

  return (
    <div className="rounded-[6px] bg-black/20 p-1.5">
      <div className="mb-1 truncate text-[0.64rem] text-fg-dim" title={overlay.channel}>
        {overlay.channel}
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} className="h-11 w-full" role="img">
        <path d={path(overlay.actual)} fill="none" stroke="#eef3f8" strokeWidth="1.4" />
        <path
          d={path(overlay.reconstructed)}
          fill="none"
          stroke="#4fd6e0"
          strokeDasharray="4 3"
          strokeWidth="1.4"
        />
      </svg>
    </div>
  );
}

function extractChannelEvidence(event: AnomalyEvent): ChannelEvidence[] {
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

function extractHeatmap(
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

function extractOverlays(
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

function extractResiduals(detail: Record<string, unknown>): ResidualEvidence[] {
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

function numberMap(value: unknown): Record<string, number> | null {
  const obj = record(value);
  if (!obj) return null;
  const out: Record<string, number> = {};
  for (const [key, raw] of Object.entries(obj)) {
    const n = numberOrNull(raw);
    if (n !== null) out[key] = n;
  }
  return Object.keys(out).length > 0 ? out : null;
}

function numberSeriesMap(value: unknown): Record<string, number[]> | null {
  const obj = record(value);
  if (!obj) return null;
  const out: Record<string, number[]> = {};
  for (const [key, raw] of Object.entries(obj)) {
    const series = numberArray(raw);
    if (series.length > 0) out[key] = series;
  }
  return Object.keys(out).length > 0 ? out : null;
}

function numberArray(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return value.filter((v): v is number => typeof v === "number" && Number.isFinite(v));
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function maxOf(values: number[]): number {
  return Math.max(0, ...values);
}

function heatColor(value: number, max: number): string {
  const t = Math.max(0, Math.min(1, value / (max || 1)));
  const r = Math.round(31 + (255 - 31) * t);
  const g = Math.round(38 + (111 - 38) * t);
  const b = Math.round(45 + (111 - 45) * t);
  return `rgb(${r}, ${g}, ${b})`;
}

function severityTone(severity: string): string {
  if (severity === "critical") return "text-bad";
  if (severity === "warning") return "text-warn";
  return "text-ok";
}

function formatNumber(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 100) return value.toFixed(0);
  if (abs >= 10) return value.toFixed(1);
  return value.toFixed(3);
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString();
  } catch {
    return ts;
  }
}

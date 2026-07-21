import {
  type ChannelEvidence,
  type EvidenceSource,
  type HeatmapData,
  type OverlaySeries,
  type ResidualEvidence,
  SOURCE_LABEL,
  SOURCES,
  extractChannelEvidence,
  extractHeatmap,
  extractOverlays,
  extractResiduals,
  heatColor,
  isEvidenceSource,
  severityTone,
} from "../lib/evidence";
import { MeterBar } from "../components/MetricTile";
import { badgeClass, cn, placeholderClass } from "../styles";
import type { AnomalyEvent } from "../types";
import { formatNumber as formatNum } from "../util/format";
import { formatTimestamp } from "../util/time";
import { RescoreControl } from "./RescoreControl";

/** Evidence cards show three decimals on sub-10 scores/residuals. */
const formatNumber = (value: number): string => formatNum(value, 3);

interface ModelEvidencePanelProps {
  event: AnomalyEvent | null;
  relatedEvents: AnomalyEvent[];
  /** Token-gated "re-score this window" (Task 4.1); omitted/empty hides
   *  the control entirely, same convention as `AnomalyPanel`'s feedback
   *  buttons. */
  token?: string;
}

export function ModelEvidencePanel({
  event,
  relatedEvents,
  token,
}: ModelEvidencePanelProps) {
  if (!event) return null;

  const sourceEvents = new Map<EvidenceSource, AnomalyEvent>();
  for (const source of SOURCES) {
    const match = relatedEvents.find((e) => e.source === source);
    if (match) sourceEvents.set(source, match);
  }
  if (isEvidenceSource(event.source)) {
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

      <RescoreControl token={token} event={event} />

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
        return (
          <div key={c.channel} className="grid grid-cols-[minmax(5rem,1fr)_5rem] gap-2">
            <div className="min-w-0">
              <div className="mb-0.5 truncate text-[0.68rem] text-fg-dim" title={c.channel}>
                {c.channel}
              </div>
              <MeterBar
                value={value}
                max={max}
                colorClass={
                  value >= 1 ? "bg-bad" : value >= 0.75 ? "bg-warn" : "bg-accent-2"
                }
              />
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

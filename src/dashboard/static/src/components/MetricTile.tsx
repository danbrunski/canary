// Two primitives that were duplicated across the metric panels:
//  - MetricTile: the label-over-value stat box (ModelHealthPanel's
//    `MetricTile` and TrainingPanel's `QualityMetric` were the same shape
//    at two sizes).
//  - MeterBar: the clamped horizontal bar track+fill (identical markup in
//    ModelHealthPanel's `RankedBars` and ModelEvidencePanel's
//    `ChannelBars`; each still owns its own colour/threshold rule).

import { cn } from "../styles";
import { type Tone, toneClass } from "../util/format";

const TILE_SIZE = {
  lg: { box: "bg-elev/70 p-2.5", label: "text-[0.62rem]", value: "text-[1.25rem]" },
  sm: { box: "bg-black/20 p-2", label: "text-[0.6rem]", value: "text-[0.9rem]" },
} as const;

export function MetricTile({
  label,
  value,
  tone,
  size = "lg",
}: {
  label: string;
  value: string;
  tone?: Tone;
  size?: keyof typeof TILE_SIZE;
}) {
  const s = TILE_SIZE[size];
  return (
    <div className={cn("rounded-[8px] border border-border-soft", s.box)}>
      <div className={cn("uppercase tracking-[0.12em] text-fg-faint", s.label)}>
        {label}
      </div>
      <div className={cn("mt-1 font-num tabular-nums", s.value, toneClass(tone))}>
        {value}
      </div>
    </div>
  );
}

/** Clamped [3%, 100%] bar. The caller picks `colorClass` so each panel
 *  keeps its own threshold rule. */
export function MeterBar({
  value,
  max,
  colorClass,
}: {
  value: number;
  max: number;
  colorClass: string;
}) {
  const width = `${Math.max(3, Math.min(100, (value / max) * 100))}%`;
  return (
    <div className="h-2 rounded-full bg-black/30">
      <div className={cn("h-2 rounded-full", colorClass)} style={{ width }} />
    </div>
  );
}

import { useMemo, useState } from "react";

import { MultiSeriesChart, type ChartSeries } from "../components/MultiSeriesChart";
import { Panel } from "../components/Panel";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { buttonClass, cn, inputClass, placeholderClass } from "../styles";

interface ChartsPanelProps {
  channels: Map<string, ChannelSnapshot>;
}

const PALETTE = [
  "#ffb547",
  "#4fd6e0",
  "#5fe3b3",
  "#ff6f6f",
  "#c08cff",
  "#8ad15a",
];
const MAX_SERIES = 6;
const WINDOWS = [60, 120, 300] as const;

/** Advanced live charting (Task 6.10): pick up to six numeric channels and
 *  trend them together with pause/resume + a time window, min/max/avg in
 *  the legend. Charts any numeric channel uniformly (raw OBD, decoded CAN,
 *  derived) since they all arrive as ring buffers on the telemetry hook. */
export function ChartsPanel({ channels }: ChartsPanelProps) {
  const numericKeys = useMemo(
    () =>
      [...channels.entries()]
        .filter(([, s]) => s.history.length > 1)
        .map(([k]) => k)
        .sort(),
    [channels],
  );

  const [selected, setSelected] = useState<string[]>([]);
  const [windowN, setWindowN] = useState<number>(120);
  const [paused, setPaused] = useState(false);
  const [filter, setFilter] = useState("");

  // While paused, freeze the data by snapshotting once.
  const [frozen, setFrozen] = useState<Map<string, number[]> | null>(null);

  const togglePause = () => {
    if (!paused) {
      const snap = new Map<string, number[]>();
      for (const key of selected) {
        snap.set(key, channels.get(key)?.history.slice() ?? []);
      }
      setFrozen(snap);
    } else {
      setFrozen(null);
    }
    setPaused((p) => !p);
  };

  const toggle = (key: string) =>
    setSelected((prev) =>
      prev.includes(key)
        ? prev.filter((k) => k !== key)
        : prev.length >= MAX_SERIES
          ? prev
          : [...prev, key],
    );

  const series: ChartSeries[] = selected.map((key, i) => {
    const all = paused
      ? (frozen?.get(key) ?? [])
      : (channels.get(key)?.history ?? []);
    return { key, color: PALETTE[i % PALETTE.length], values: all.slice(-windowN) };
  });

  const shown = filter
    ? numericKeys.filter((k) => k.toLowerCase().includes(filter.toLowerCase()))
    : numericKeys;

  return (
    <Panel
      title="Charts"
      count={`${selected.length}/${MAX_SERIES} selected`}
      actions={
        <div className="flex items-center gap-2 text-[0.76rem] text-fg-dim">
          <select
            className={cn(inputClass, "min-h-0 py-0.5")}
            value={windowN}
            onChange={(e) => setWindowN(Number(e.target.value))}
            aria-label="Time window (samples)"
          >
            {WINDOWS.map((w) => (
              <option key={w} value={w}>
                last {w}
              </option>
            ))}
          </select>
          <button
            type="button"
            className={cn(buttonClass, "px-3 py-1", paused && "border-accent text-accent")}
            onClick={togglePause}
          >
            {paused ? "▶ resume" : "⏸ pause"}
          </button>
        </div>
      }
    >
      {numericKeys.length === 0 ? (
        <p className={placeholderClass}>
          no numeric channels with history yet — waiting for telemetry.
        </p>
      ) : (
        <>
          {series.length > 0 ? (
            <MultiSeriesChart series={series} height={240} />
          ) : (
            <p className={cn(placeholderClass, "h-[240px]")}>
              select channels below to chart them.
            </p>
          )}

          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[0.78rem]">
            {series.map((s) => {
              const v = s.values;
              const last = v.at(-1);
              const min = v.length ? Math.min(...v) : null;
              const max = v.length ? Math.max(...v) : null;
              const avg = v.length
                ? v.reduce((a, b) => a + b, 0) / v.length
                : null;
              return (
                <span key={s.key} className="inline-flex items-center gap-1.5">
                  <span
                    className="inline-block h-2 w-3 rounded-sm"
                    style={{ background: s.color }}
                  />
                  <b className="font-num">{s.key}</b>
                  <span className="font-num tabular-nums text-fg-dim">
                    {fmt(last)} (min {fmt(min)} / avg {fmt(avg)} / max {fmt(max)})
                  </span>
                </span>
              );
            })}
          </div>

          <div className="mt-3">
            <input
              className={cn(inputClass, "mb-2 w-full")}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="filter channels…"
              aria-label="Filter channels"
            />
            <div className="grid max-h-44 grid-cols-[repeat(auto-fill,minmax(13rem,1fr))] gap-1 overflow-y-auto">
              {shown.map((key) => {
                const on = selected.includes(key);
                return (
                  <button
                    key={key}
                    type="button"
                    className={cn(
                      "truncate rounded border px-2 py-1 text-left text-[0.78rem]",
                      on
                        ? "border-accent bg-accent/10 text-fg"
                        : "border-border-soft text-fg-dim hover:border-border",
                    )}
                    onClick={() => toggle(key)}
                    disabled={!on && selected.length >= MAX_SERIES}
                  >
                    {on ? "● " : "○ "}
                    {key}
                  </button>
                );
              })}
            </div>
          </div>
        </>
      )}
    </Panel>
  );
}

function fmt(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return Math.abs(v) >= 1000 ? v.toFixed(0) : v.toFixed(2);
}

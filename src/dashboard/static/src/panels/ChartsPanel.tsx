import { useMemo, useState } from "react";

import {
  downloadSessionGpx,
  downloadSessionKml,
  getSessionChart,
} from "../api/http";
import { MultiSeriesChart, type ChartSeries } from "../components/MultiSeriesChart";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { useAsyncAction } from "../hooks/useAsyncAction";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { buttonClass, cn, inputClass, placeholderClass } from "../styles";
import type { SessionChartResponse } from "../types";

interface ChartsPanelProps {
  channels: Map<string, ChannelSnapshot>;
  /** Control token — only needed for session mode's authenticated
   *  `/control/sessions/{id}/chart` + GPX/KML reads. Live mode (the
   *  default) never needs it, so it's optional. */
  token?: string;
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

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

/** Advanced charting (Task 6.10, session mode Task 5.3): pick up to six
 *  numeric channels and trend them together. "Live" mode charts the current
 *  telemetry stream's ring buffers (unchanged from 6.10); "Session" mode
 *  loads a *stored* session via `GET /control/sessions/{id}/chart` instead —
 *  the historical-session-charting half of Task 5.3 (also exposes the
 *  GPX/KML GPS-track export, Task 5.6a, since a session is already picked
 *  here). */
const Y_AXIS_MODES = ["independent", "shared"] as const;

export function ChartsPanel({ channels, token = "" }: ChartsPanelProps) {
  const [mode, setMode] = useState<"live" | "session">("live");
  // Task 5.5: per-series self-normalization ("independent") hides relative
  // magnitude between channels; "shared" plots every series against one
  // global min/max so they're actually comparable. MultiSeriesChart already
  // implements both — this toggle is what makes "shared" reachable.
  const [yAxisMode, setYAxisMode] = useState<(typeof Y_AXIS_MODES)[number]>("independent");

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

  // Session mode state.
  const [sessionId, setSessionId] = useState("");
  const [sessionChannels, setSessionChannels] = useState("");
  const [resampleHz, setResampleHz] = useState("1");
  const [sessionData, setSessionData] = useState<SessionChartResponse | null>(null);
  const { notice, busy, run } = useAsyncAction();

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

  const loadSession = () =>
    run(
      "load session",
      async () => {
        if (!token) throw new Error("Enter the control token first.");
        if (!sessionId.trim()) throw new Error("Enter a session id.");
        const keys = sessionChannels
          .split(",")
          .map((k) => k.trim())
          .filter(Boolean);
        if (keys.length === 0) throw new Error("Enter at least one channel.");
        const hz = Number(resampleHz);
        const next = await getSessionChart(
          token,
          sessionId.trim(),
          keys,
          Number.isFinite(hz) && hz > 0 ? hz : undefined,
        );
        setSessionData(next);
      },
      { okText: null },
    );

  const exportTrack = (format: "gpx" | "kml") =>
    run(`export ${format}`, async () => {
      if (!token) throw new Error("Enter the control token first.");
      if (!sessionId.trim()) throw new Error("Enter a session id.");
      const blob =
        format === "gpx"
          ? await downloadSessionGpx(token, sessionId.trim())
          : await downloadSessionKml(token, sessionId.trim());
      downloadBlob(blob, `${sessionId.trim()}.${format}`);
    });

  const liveSeries: ChartSeries[] = selected.map((key, i) => {
    const all = paused
      ? (frozen?.get(key) ?? [])
      : (channels.get(key)?.history ?? []);
    return { key, color: PALETTE[i % PALETTE.length], values: all.slice(-windowN) };
  });

  const sessionKeys = sessionData ? Object.keys(sessionData.channels) : [];
  const sessionSeries: ChartSeries[] = sessionKeys.map((key, i) => ({
    key,
    color: PALETTE[i % PALETTE.length],
    values: (sessionData?.channels[key].values ?? []).map((v) => (v == null ? NaN : v)),
  }));
  const sessionTimestamps = sessionKeys.length
    ? sessionData?.channels[sessionKeys[0]].timestamps
    : undefined;

  const series = mode === "session" ? sessionSeries : liveSeries;

  const shown = filter
    ? numericKeys.filter((k) => k.toLowerCase().includes(filter.toLowerCase()))
    : numericKeys;

  return (
    <Panel
      title="Charts"
      count={mode === "live" ? `${selected.length}/${MAX_SERIES} selected` : undefined}
      actions={
        <div className="flex items-center gap-2 text-[0.76rem] text-fg-dim">
          <div className="flex overflow-hidden rounded border border-border-soft">
            {(["live", "session"] as const).map((m) => (
              <button
                key={m}
                type="button"
                className={cn(
                  "px-2 py-0.5 capitalize",
                  mode === m ? "bg-accent/20 text-fg" : "text-fg-dim",
                )}
                onClick={() => setMode(m)}
              >
                {m}
              </button>
            ))}
          </div>
          <div className="flex overflow-hidden rounded border border-border-soft">
            {Y_AXIS_MODES.map((y) => (
              <button
                key={y}
                type="button"
                className={cn(
                  "px-2 py-0.5 capitalize",
                  yAxisMode === y ? "bg-accent/20 text-fg" : "text-fg-dim",
                )}
                onClick={() => setYAxisMode(y)}
              >
                {y}
              </button>
            ))}
          </div>
          {mode === "live" && (
            <>
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
            </>
          )}
        </div>
      }
    >
      {mode === "session" && (
        <div className="mb-3 flex flex-wrap items-end gap-2">
          <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
            session id
            <input
              className={inputClass}
              value={sessionId}
              onChange={(e) => setSessionId(e.target.value)}
              placeholder="2026-07-19T000000"
            />
          </label>
          <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
            channels (comma-separated)
            <input
              className={cn(inputClass, "w-64")}
              value={sessionChannels}
              onChange={(e) => setSessionChannels(e.target.value)}
              placeholder="obd:RPM,obd:ENGINE_LOAD"
            />
          </label>
          <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
            resample hz
            <input
              className={cn(inputClass, "w-20")}
              value={resampleHz}
              onChange={(e) => setResampleHz(e.target.value)}
            />
          </label>
          <button
            type="button"
            className={cn(buttonClass, "px-3 py-1")}
            disabled={Boolean(busy)}
            onClick={loadSession}
          >
            {busy === "load session" ? "loading…" : "load"}
          </button>
          <button
            type="button"
            className={cn(buttonClass, "px-3 py-1")}
            disabled={Boolean(busy)}
            onClick={() => exportTrack("gpx")}
          >
            GPX
          </button>
          <button
            type="button"
            className={cn(buttonClass, "px-3 py-1")}
            disabled={Boolean(busy)}
            onClick={() => exportTrack("kml")}
          >
            KML
          </button>
        </div>
      )}

      {notice && <Notice notice={notice} className="mb-2" />}

      {mode === "live" && numericKeys.length === 0 ? (
        <p className={placeholderClass}>
          no numeric channels with history yet — waiting for telemetry.
        </p>
      ) : (
        <>
          {series.length > 0 ? (
            <MultiSeriesChart
              series={series}
              height={240}
              timestamps={sessionTimestamps}
              yAxisMode={yAxisMode}
            />
          ) : (
            <p className={cn(placeholderClass, "h-[240px]")}>
              {mode === "session"
                ? "load a session above to chart it."
                : "select channels below to chart them."}
            </p>
          )}

          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[0.78rem]">
            {series.map((s) => {
              const v = s.values.filter((x) => Number.isFinite(x));
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

          {mode === "live" && (
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
          )}
        </>
      )}
    </Panel>
  );
}

function fmt(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return Math.abs(v) >= 1000 ? v.toFixed(0) : v.toFixed(2);
}

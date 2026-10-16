import { useMemo, useState } from "react";

import { cssVar } from "../../lib/chartTheme";
import { sequential, sequentialIn } from "../../lib/colorScale";
import { HEAT_BINS, extent, heatCells, heatShade, scatterPoints } from "../../lib/framePlot";
import { formatFrameValue } from "../../lib/frameTable";
import { buttonClass, selectClass } from "../../styles";
import type { FrameResponse } from "../../types/workbench";

const W = 360;
const H = 240;

/** One shared empty column, so a missing channel keeps the memos stable. */
const NO_VALUES: Array<number | null> = [];

/** Any two channels of a frame against each other, as a scatter (optionally
 *  coloured by a third) or a 12 x 12 heat map of sample counts or of the
 *  third channel's mean. The axis choices are this view's own state. */
export function ScatterView({ frame }: { frame: FrameResponse }) {
  const specs = frame.columns.map((c) => c.spec);
  const [xChoice, setX] = useState(specs[0] ?? "");
  const [yChoice, setY] = useState(specs[1] ?? specs[0] ?? "");
  const [zChoice, setZ] = useState(specs[2] ?? "");
  const [mode, setMode] = useState<"scatter" | "heat">("scatter");
  // A choice the frame no longer carries falls back to the default axis.
  const x = specs.includes(xChoice) ? xChoice : (specs[0] ?? "");
  const y = specs.includes(yChoice) ? yChoice : (specs[1] ?? specs[0] ?? "");
  const z = zChoice && specs.includes(zChoice) ? zChoice : "";

  const xs = frame.values[x] ?? NO_VALUES;
  const ys = frame.values[y] ?? NO_VALUES;
  const zs = z ? (frame.values[z] ?? NO_VALUES) : NO_VALUES;
  const xr = useMemo(() => extent(xs), [xs]);
  const yr = useMemo(() => extent(ys), [ys]);
  const zr = useMemo(() => extent(zs), [zs]);
  const color = (v: number | null) =>
    v === null || !zr ? cssVar("accent-2") : sequentialIn(v, zr[0], zr[1]);

  const heat = useMemo(
    () => (mode === "heat" ? heatCells(xs, ys, z ? zs : null, xr, yr) : []),
    [mode, xs, ys, zs, z, xr, yr],
  );
  const heatRange = extent(heat.map((c) => c.value));
  const points = mode === "scatter" ? scatterPoints(xs, ys, xr, yr, W, H) : [];

  return (
    <figure className="m-0">
      <div className="mb-1 flex flex-wrap items-end gap-2 text-xs">
        {(
          [
            ["X", x, setX],
            ["Y", y, setY],
          ] as const
        ).map(([label, value, set]) => (
          <label key={label} className="flex flex-col gap-1">
            {label}
            <select
              className={selectClass}
              aria-label={`${label} axis`}
              value={value}
              onChange={(e) => set(e.target.value)}
            >
              {specs.map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          </label>
        ))}
        <label className="flex flex-col gap-1">
          {mode === "heat" ? "Cell value (mean)" : "Colour"}
          <select
            className={selectClass}
            aria-label="Colour channel"
            value={z}
            onChange={(e) => setZ(e.target.value)}
          >
            <option value="">{mode === "heat" ? "sample count" : "none"}</option>
            {specs.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className={buttonClass}
          onClick={() => setMode(mode === "scatter" ? "heat" : "scatter")}
        >
          {mode === "scatter" ? "Heat map" : "Scatter"}
        </button>
      </div>
      <svg
        aria-label={mode === "scatter" ? "Scatter" : "Heat map"}
        width={W}
        height={H}
        className="bg-elev-2"
      >
        {points.map((point) => (
          <circle
            key={point.index}
            cx={point.cx}
            cy={point.cy}
            r={1.8}
            style={{ fill: color(z ? (zs[point.index] ?? null) : null) }}
          />
        ))}
        {heat.map((cell) => {
          const w = W / HEAT_BINS;
          const h = H / HEAT_BINS;
          return (
            <rect
              key={`${cell.bx}:${cell.by}`}
              x={cell.bx * w}
              y={H - (cell.by + 1) * h}
              width={w}
              height={h}
              fill={sequential(heatShade(cell.value, heatRange))}
            >
              <title>{`${formatFrameValue(cell.value)} (n ${cell.n})`}</title>
            </rect>
          );
        })}
      </svg>
      <figcaption className="text-[0.65rem] text-fg-faint">
        {y} against {x}
        {z
          ? `, ${mode === "heat" ? "cell mean of" : "coloured by"} ${z}`
          : mode === "heat"
            ? ", cell sample count"
            : ""}
        {xr && ` · x ${formatFrameValue(xr[0])}–${formatFrameValue(xr[1])}`}
        {yr && ` · y ${formatFrameValue(yr[0])}–${formatFrameValue(yr[1])}`}
      </figcaption>
    </figure>
  );
}

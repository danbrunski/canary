import { useMemo } from "react";

import { seriesVar } from "../../lib/chartTheme";
import { extent, stripMarkers, stripPoints, timeAxis, timeX } from "../../lib/framePlot";
import { formatFrameValue } from "../../lib/frameTable";
import type { FrameResponse } from "../../types/workbench";
import { ScatterView } from "./ScatterView";

const STRIP_W = 720;
const STRIP_H = 70;

/** A frame as one time-series strip per channel on a shared time axis, with
 *  the operator's tags and calibration flashes marked on every strip, and a
 *  scatter / heat map of any two channels below. */
export function FrameVisual({ frame }: { frame: FrameResponse }) {
  const axis = useMemo(() => timeAxis(frame.timestamps), [frame]);
  const markers = stripMarkers(frame, axis);

  if (frame.rows_total === 0) {
    return <p className="m-0 text-sm text-fg-dim">No rows in this view.</p>;
  }
  return (
    <div className="space-y-3">
      <div aria-label="Time-series strips" className="space-y-1">
        {frame.columns.map((column, index) => {
          const values = frame.values[column.spec] ?? [];
          const range = extent(values);
          const points = stripPoints(values, axis, range, STRIP_W, STRIP_H);
          return (
            <figure key={column.spec} className="m-0">
              <figcaption className="text-[0.65rem] text-fg-dim">
                {column.spec}
                {column.unit ? ` (${column.unit})` : ""}
                {range && ` · ${formatFrameValue(range[0])}–${formatFrameValue(range[1])}`}
              </figcaption>
              <svg
                width="100%"
                viewBox={`0 0 ${STRIP_W} ${STRIP_H}`}
                preserveAspectRatio="none"
                className="h-16 bg-elev-2"
              >
                <polyline
                  points={points}
                  fill="none"
                  style={{ stroke: seriesVar(index) }}
                  strokeWidth={1.2}
                />
                {markers.map((m, i) => (
                  <line
                    key={`${m.kind}-${i}`}
                    data-marker={m.kind}
                    x1={timeX(axis, m.t, STRIP_W)}
                    x2={timeX(axis, m.t, STRIP_W)}
                    y1={0}
                    y2={STRIP_H}
                    className={m.kind === "flash" ? "stroke-bad" : "stroke-fg-dim"}
                    strokeDasharray={m.kind === "flash" ? undefined : "3 3"}
                  >
                    <title>{m.label}</title>
                  </line>
                ))}
              </svg>
            </figure>
          );
        })}
      </div>
      <ScatterView frame={frame} />
    </div>
  );
}

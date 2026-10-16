import { DELIVERY_CHART_HEIGHT, deliveryScale } from "../../lib/analysisPlot";
import type { DeliveryErrorResponse } from "../../types/workbench";

/** Delivery error per rpm band as bars about a zero line, with n on every
 *  bar and the noise floor behind them: a bar inside the sd band is grey, so
 *  an error the floor cannot tell from nothing does not read as a finding. */
export function DeliveryChart({ result }: { result: DeliveryErrorResponse }) {
  const floor = result.noise_floor;
  const { sd, rangeLo, rangeHi, width: W, y } = deliveryScale(result);
  const H = DELIVERY_CHART_HEIGHT;
  const bw = W / Math.max(1, result.bands.length);
  return (
    <svg aria-label="Delivery error by rpm band" width={W} height={H + 20} className="bg-elev-2">
      {floor.available && (
        <>
          <rect
            data-floor="range"
            x={0}
            width={W}
            y={y(rangeHi)}
            height={y(rangeLo) - y(rangeHi)}
            className="fill-fg-dim/10"
          />
          <rect
            data-floor="sd"
            x={0}
            width={W}
            y={y(sd)}
            height={y(-sd) - y(sd)}
            className="fill-fg-dim/20"
          />
        </>
      )}
      <line x1={0} x2={W} y1={y(0)} y2={y(0)} className="stroke-fg-dim" />
      {result.bands.map((band, i) => {
        const v = band.error_pct ?? 0;
        const top = Math.min(y(v), y(0));
        const inside = floor.available && v >= -sd && v <= sd;
        return (
          <g key={band.rpm_band} data-band={band.rpm_band}>
            <rect
              x={i * bw + bw * 0.2}
              width={bw * 0.6}
              y={top}
              height={Math.abs(y(v) - y(0))}
              className={inside ? "fill-fg-dim" : v > 0 ? "fill-accent" : "fill-accent-2"}
            />
            <text
              x={i * bw + bw / 2}
              y={v >= 0 ? top - 3 : top + Math.abs(y(v) - y(0)) + 10}
              textAnchor="middle"
              fontSize={10}
              className="fill-fg"
            >
              {v.toFixed(2)}% · n {band.n}
            </text>
            <text
              x={i * bw + bw / 2}
              y={H + 14}
              textAnchor="middle"
              fontSize={9}
              className="fill-fg-dim"
            >
              {band.rpm_band}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

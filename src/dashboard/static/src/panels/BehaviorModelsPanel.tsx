import type { ReactNode } from "react";

import { Curve } from "../components/Curve";
import { Heatmap } from "../components/Heatmap";
import { Sparkline } from "../components/Sparkline";
import {
  SEVERITY_HEX,
  SEVERITY_TEXT,
  binIndex,
  fmt,
} from "../lib/behaviorOverlay";
import {
  cn,
  panelClass,
  panelCountClass,
  panelHeaderClass,
  panelTitleClass,
  placeholderClass,
} from "../styles";
import type {
  BehaviorLiveModel,
  BehaviorLiveResponse,
  BehaviorModel,
  BehaviorModel1D,
  BehaviorModel2D,
  BehaviorModelsResponse,
} from "../types";

interface BehaviorModelsPanelProps {
  data: BehaviorModelsResponse | null;
  error: Error | null;
  live: BehaviorLiveResponse | null;
}

/** Behavior-model surfaces panel (Task 4.E.9) with the live overlay.
 *
 *  Each fitted surface from `data/behavior_models.yaml` renders as before
 *  (1D curve / 2D heatmap), but now with a "you are here" layer fed by
 *  `/models/behavior/live`: the daemon evaluates every surface each
 *  inference tick, so the marker, deviation envelope, recent trajectory,
 *  and Δ sparkline reflect *backend truth* — mode-gated, domain-clamped,
 *  empty-cell aware — rather than a client-side re-derivation.
 *
 *  This turns the dashboard's "what does normal look like?" reference into
 *  "where are we relative to normal, right now?" — the same deviation the
 *  IF/LSTM consume, made glanceable. */
export function BehaviorModelsPanel({
  data,
  error,
  live,
}: BehaviorModelsPanelProps) {
  if (error) {
    return (
      <Frame>
        <p className="rounded-md bg-bad/12 px-2 py-1.5 text-[0.82rem] text-bad">
          /models/behavior unreachable: {error.message}
        </p>
      </Frame>
    );
  }
  if (data === null) {
    return (
      <Frame>
        <p className={placeholderClass}>loading…</p>
      </Frame>
    );
  }
  if (data.models.length === 0) {
    return (
      <Frame>
        <p className={placeholderClass}>
          no surfaces fitted yet — run{" "}
          <code>scripts/refit_behavior_models.py</code> once a session is
          on disk.
        </p>
      </Frame>
    );
  }
  const mode = live?.operating_mode ?? null;
  return (
    <Frame
      count={`${data.models.length} surfaces`}
      mode={mode}
      streaming={Boolean(live && live.observed > 0)}
    >
      <div className="grid grid-cols-[repeat(auto-fit,minmax(280px,1fr))] gap-3">
        {data.models.map((m) => (
          <ModelCard
            key={`${m.name}-v${m.version}`}
            model={m}
            live={live?.models?.[m.name] ?? null}
            mode={mode}
          />
        ))}
      </div>
    </Frame>
  );
}

function Frame({
  children,
  count,
  mode,
  streaming,
}: {
  children: ReactNode;
  count?: string;
  mode?: string | null;
  streaming?: boolean;
}) {
  return (
    <section className={cn(panelClass, "col-span-2 max-[1100px]:col-span-1")}>
      <header className={panelHeaderClass}>
        <h2 className={panelTitleClass}>Behavior Models</h2>
        <div className="flex items-center gap-2">
          {mode && (
            <span className="font-num text-[0.7rem] tabular-nums text-fg-dim">
              mode <span className="text-fg">{mode}</span>
            </span>
          )}
          {streaming && (
            <span
              className="h-1.5 w-1.5 rounded-full bg-ok"
              title="live overlay streaming"
            />
          )}
          {count && <span className={panelCountClass}>{count}</span>}
        </div>
      </header>
      {children}
    </section>
  );
}

function ModelCard({
  model,
  live,
  mode,
}: {
  model: BehaviorModel;
  live: BehaviorLiveModel | null;
  mode: string | null;
}) {
  // Gated: the model only evaluates in its operating_mode and we're not in
  // it. Showing a "you are here" marker would be a lie (the decoder returns
  // None), so we suppress the live layer and say why.
  const gated = Boolean(live && !live.mode_match);
  const active = live && live.mode_match ? live : null;
  // Active but the model didn't speak — off the fitted domain or an
  // unobserved grid cell. The deviation columns the IF/LSTM see are NaN
  // here, which is exactly why anomalies get noisy at the envelope edge.
  const offMap = Boolean(active && active.expected === null);

  return (
    <article
      className={cn(
        "rounded-[8px] border border-border bg-elev-2 px-3 py-2.5",
        gated && "opacity-55",
      )}
    >
      <header className="mb-1 flex items-baseline justify-between gap-2">
        <h3 className="m-0 text-[0.92rem] font-semibold tracking-[0.02em] text-fg">
          {model.name}
        </h3>
        <span className="font-num text-[0.7rem] tabular-nums text-fg-dim">
          v{model.version}
        </span>
      </header>
      <div className="mb-2 flex flex-wrap gap-x-3.5 gap-y-2 font-num text-[0.75rem] tabular-nums text-fg-dim">
        <span>
          <em className="mr-1 not-italic text-fg-dim">out</em> {model.output}{" "}
          {model.output_unit !== "unknown" && (
            <span className="text-fg-dim">({model.output_unit})</span>
          )}
        </span>
        {model.operating_mode && <span>mode {model.operating_mode}</span>}
        {model.fit.tune_revision && <span>tune {model.fit.tune_revision}</span>}
        <span>n = {model.fit.sample_count.toLocaleString()}</span>
        {model.fit.r_squared !== null && (
          <span>r² {model.fit.r_squared.toFixed(3)}</span>
        )}
      </div>
      {(model.conditions ?? []).length > 0 && (
        <p className="mb-2 text-[0.72rem] text-fg-dim">
          Steady-state only: {(model.conditions ?? []).map((condition) => (
            `${condition.channel} ≤ ±${condition.max_abs_per_second}/s`
          )).join(", ")}
        </p>
      )}

      {model.kind === "surface_1d" ? (
        <Surface1DView model={model} live={active} />
      ) : (
        <Surface2DView model={model} live={active} />
      )}

      <LiveReadout
        live={active}
        unit={model.output_unit}
        gated={gated}
        gatedMode={model.operating_mode}
        currentMode={mode}
        offMap={offMap}
      />

      {model.notes && (
        <p className="mt-2 mb-0 text-[0.75rem] text-fg-dim italic">
          {model.notes}
        </p>
      )}
    </article>
  );
}

/** Numeric expected/actual/Δ + severity chip + Δ sparkline, or the reason
 *  the live layer is absent (gated / off-map / no daemon). */
function LiveReadout({
  live,
  unit,
  gated,
  gatedMode,
  currentMode,
  offMap,
}: {
  live: BehaviorLiveModel | null;
  unit: string;
  gated: boolean;
  gatedMode: string | null;
  currentMode: string | null;
  offMap: boolean;
}) {
  if (gated) {
    return (
      <p className="mt-2 mb-0 text-[0.72rem] text-fg-dim italic">
        gated — evaluates in <span className="text-fg">{gatedMode}</span>, now{" "}
        <span className="text-fg">{currentMode ?? "—"}</span>
      </p>
    );
  }
  if (!live) return null;
  if (offMap) {
    return (
      <p className="mt-2 mb-0 text-[0.72rem] text-fg-dim italic">
        off-map — operating point outside the fitted envelope (deviation
        unavailable, anomaly scoring blind here)
      </p>
    );
  }

  const u = unit !== "unknown" ? ` ${unit}` : "";
  const devSeries = live.series
    .map((p) => p.deviation)
    .filter((d): d is number => d !== null && Number.isFinite(d));

  return (
    <div className="mt-2 flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-num text-[0.74rem] tabular-nums">
        <span className="text-fg-dim">
          exp <span className="text-fg">{fmt(live.expected)}</span>
        </span>
        <span className="text-fg-dim">
          act <span className="text-fg">{fmt(live.actual)}</span>
        </span>
        <span className="text-fg-dim">
          Δ{" "}
          <span className={SEVERITY_TEXT[live.severity]}>
            {live.deviation !== null && live.deviation > 0 ? "+" : ""}
            {fmt(live.deviation)}
            {u}
          </span>
        </span>
        <span
          className={cn(
            "rounded px-1.5 py-0.5 text-[0.64rem] font-semibold tracking-[0.04em] uppercase",
            live.severity === "ok" && "bg-ok/15 text-ok",
            live.severity === "warning" && "bg-warn/15 text-warn",
            live.severity === "critical" && "bg-bad/15 text-bad",
          )}
        >
          {live.severity}
        </span>
      </div>
      {devSeries.length >= 2 && (
        <div className="flex items-center gap-2">
          <span className="font-num text-[0.64rem] text-fg-faint">Δ trail</span>
          <Sparkline
            values={devSeries}
            width={150}
            height={22}
            stroke={SEVERITY_HEX[live.severity]}
          />
        </div>
      )}
    </div>
  );
}

function Surface1DView({
  model,
  live,
}: {
  model: BehaviorModel1D;
  live: BehaviorLiveModel | null;
}) {
  return (
    <div className="flex flex-col gap-1">
      <Curve
        fit={model.fit}
        domain={model.axis.domain}
        deviation={model.deviation}
        live={
          live
            ? {
                x: live.x,
                actual: live.actual,
                expected: live.expected,
                severity: live.severity,
                trail: live.series.map((p) => ({
                  x: p.x,
                  actual: p.actual,
                })),
              }
            : null
        }
      />
      <div className="flex gap-3.5 font-num text-[0.7rem] tabular-nums text-fg-dim">
        {model.axis.name}{" "}
        <span className="text-fg-dim">
          ({model.axis.unit}) [{model.axis.domain[0].toFixed(0)}–
          {model.axis.domain[1].toFixed(0)}]
        </span>
      </div>
    </div>
  );
}

function Surface2DView({
  model,
  live,
}: {
  model: BehaviorModel2D;
  live: BehaviorLiveModel | null;
}) {
  const [xAxis, yAxis] = model.axes;
  const rows = xAxis.bin_edges.length - 1; // outer (axes[0]) — RPM-ish
  const cols = yAxis.bin_edges.length - 1; // inner (axes[1]) — load-ish

  // Coverage of the cell we're operating in right now: a low count here
  // explains a jumpy deviation feature better than any global metric.
  let coverage: number | null = null;
  if (live && live.x !== null && live.y !== null) {
    const ri = binIndex(live.x, xAxis.bin_edges);
    const ci = binIndex(live.y, yAxis.bin_edges);
    if (ri !== null && ci !== null) {
      coverage = model.fit.cells[ri * cols + ci]?.count ?? null;
    }
  }

  return (
    <div className="flex flex-col gap-1">
      <Heatmap
        cells={model.fit.cells}
        rows={rows}
        cols={cols}
        xEdges={xAxis.bin_edges}
        yEdges={yAxis.bin_edges}
        live={
          live ? { x: live.x, y: live.y, severity: live.severity } : null
        }
      />
      <div className="flex justify-between gap-3.5 font-num text-[0.7rem] tabular-nums text-fg-dim">
        <span>
          ↑ {xAxis.name} <span className="text-fg-dim">({xAxis.unit})</span>
        </span>
        <span>
          → {yAxis.name} <span className="text-fg-dim">({yAxis.unit})</span>
        </span>
      </div>
      {coverage !== null && (
        <div
          className={cn(
            "font-num text-[0.68rem] tabular-nums",
            coverage < 10 ? "text-warn" : "text-fg-dim",
          )}
        >
          here: n={coverage}
          {coverage < 10 && " — sparsely sampled, deviation noisy"}
        </div>
      )}
    </div>
  );
}

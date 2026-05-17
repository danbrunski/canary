import { Curve } from "../components/Curve";
import { Heatmap } from "../components/Heatmap";
import {
  cn,
  panelClass,
  panelCountClass,
  panelHeaderClass,
  panelTitleClass,
  placeholderClass,
} from "../styles";
import type {
  BehaviorModel,
  BehaviorModel1D,
  BehaviorModel2D,
  BehaviorModelsResponse,
} from "../types";

interface BehaviorModelsPanelProps {
  data: BehaviorModelsResponse | null;
  error: Error | null;
}

/** Behavior-model surfaces panel (Task 4.E.9).
 *
 *  Renders each fitted surface from `data/behavior_models.yaml`:
 *  * 1D fits → a curve sampled across the declared domain plus R² /
 *    sample count / version. Updates whenever
 *    `scripts/refit_behavior_models.py` writes a new YAML.
 *  * 2D fits → a heatmap of per-cell mean, with empty cells (count=0)
 *    rendered as the background color so operating-envelope gaps are
 *    immediately visible.
 *
 *  This is the dashboard's window into "what does the engine think 'normal'
 *  looks like right now?" — the deviation columns the IF/LSTM see are
 *  derived from these surfaces, so a sparsely-populated heatmap explains
 *  why anomalies are noisy at the edge of the envelope. */
export function BehaviorModelsPanel({ data, error }: BehaviorModelsPanelProps) {
  if (error) {
    return (
      <section className={cn(panelClass, "col-span-2 max-[1100px]:col-span-1")}>
        <header className={panelHeaderClass}>
          <h2 className={panelTitleClass}>Behavior Models</h2>
        </header>
        <p className="rounded-md bg-bad/12 px-2 py-1.5 text-[0.82rem] text-bad">
          /models/behavior unreachable: {error.message}
        </p>
      </section>
    );
  }
  if (data === null) {
    return (
      <section className={cn(panelClass, "col-span-2 max-[1100px]:col-span-1")}>
        <header className={panelHeaderClass}>
          <h2 className={panelTitleClass}>Behavior Models</h2>
        </header>
        <p className={placeholderClass}>loading…</p>
      </section>
    );
  }
  if (data.models.length === 0) {
    return (
      <section className={cn(panelClass, "col-span-2 max-[1100px]:col-span-1")}>
        <header className={panelHeaderClass}>
          <h2 className={panelTitleClass}>Behavior Models</h2>
        </header>
        <p className={placeholderClass}>
          no surfaces fitted yet — run{" "}
          <code>scripts/refit_behavior_models.py</code> once a session is
          on disk.
        </p>
      </section>
    );
  }
  return (
    <section className={cn(panelClass, "col-span-2 max-[1100px]:col-span-1")}>
      <header className={panelHeaderClass}>
        <h2 className={panelTitleClass}>Behavior Models</h2>
        <span className={panelCountClass}>{data.models.length} surfaces</span>
      </header>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(280px,1fr))] gap-3">
        {data.models.map((m) => (
          <ModelCard key={`${m.name}-v${m.version}`} model={m} />
        ))}
      </div>
    </section>
  );
}

function ModelCard({ model }: { model: BehaviorModel }) {
  return (
    <article className="rounded-[8px] border border-border bg-elev-2 px-3 py-2.5">
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
        <span>n = {model.fit.sample_count.toLocaleString()}</span>
        {model.fit.r_squared !== null && (
          <span>r² {model.fit.r_squared.toFixed(3)}</span>
        )}
      </div>
      {model.kind === "surface_1d" ? (
        <Surface1DView model={model} />
      ) : (
        <Surface2DView model={model} />
      )}
      {model.notes && (
        <p className="mt-2 mb-0 text-[0.75rem] text-fg-dim italic">{model.notes}</p>
      )}
    </article>
  );
}

function Surface1DView({ model }: { model: BehaviorModel1D }) {
  return (
    <div className="flex flex-col gap-1">
      <Curve fit={model.fit} domain={model.axis.domain} />
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

function Surface2DView({ model }: { model: BehaviorModel2D }) {
  const [xAxis, yAxis] = model.axes;
  const rows = xAxis.bin_edges.length - 1; // outer (axes[0]) — RPM-ish
  const cols = yAxis.bin_edges.length - 1; // inner (axes[1]) — load-ish
  return (
    <div className="flex flex-col gap-1">
      <Heatmap cells={model.fit.cells} rows={rows} cols={cols} />
      <div className="flex justify-between gap-3.5 font-num text-[0.7rem] tabular-nums text-fg-dim">
        <span>
          ↑ {xAxis.name} <span className="text-fg-dim">({xAxis.unit})</span>
        </span>
        <span>
          → {yAxis.name} <span className="text-fg-dim">({yAxis.unit})</span>
        </span>
      </div>
    </div>
  );
}

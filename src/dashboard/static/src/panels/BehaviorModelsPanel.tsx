import { Curve } from "../components/Curve";
import { Heatmap } from "../components/Heatmap";
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
      <section className="panel behavior-models-panel">
        <header>
          <h2>Behavior Models</h2>
        </header>
        <p className="error">/models/behavior unreachable: {error.message}</p>
      </section>
    );
  }
  if (data === null) {
    return (
      <section className="panel behavior-models-panel">
        <header>
          <h2>Behavior Models</h2>
        </header>
        <p className="placeholder">loading…</p>
      </section>
    );
  }
  if (data.models.length === 0) {
    return (
      <section className="panel behavior-models-panel">
        <header>
          <h2>Behavior Models</h2>
        </header>
        <p className="placeholder">
          no surfaces fitted yet — run{" "}
          <code>scripts/refit_behavior_models.py</code> once a session is
          on disk.
        </p>
      </section>
    );
  }
  return (
    <section className="panel behavior-models-panel">
      <header>
        <h2>Behavior Models</h2>
        <span className="count">{data.models.length} surfaces</span>
      </header>
      <div className="model-cards">
        {data.models.map((m) => (
          <ModelCard key={`${m.name}-v${m.version}`} model={m} />
        ))}
      </div>
    </section>
  );
}

function ModelCard({ model }: { model: BehaviorModel }) {
  return (
    <article className="model-card">
      <header>
        <h3>{model.name}</h3>
        <span className="version">v{model.version}</span>
      </header>
      <div className="model-meta">
        <span>
          <em>out</em> {model.output}{" "}
          {model.output_unit !== "unknown" && (
            <span className="unit">({model.output_unit})</span>
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
      {model.notes && <p className="model-notes">{model.notes}</p>}
    </article>
  );
}

function Surface1DView({ model }: { model: BehaviorModel1D }) {
  return (
    <div className="surface-1d">
      <Curve fit={model.fit} domain={model.axis.domain} />
      <div className="axis-label">
        {model.axis.name}{" "}
        <span className="dim">
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
    <div className="surface-2d">
      <Heatmap cells={model.fit.cells} rows={rows} cols={cols} />
      <div className="axis-labels">
        <span>
          ↑ {xAxis.name} <span className="dim">({xAxis.unit})</span>
        </span>
        <span>
          → {yAxis.name} <span className="dim">({yAxis.unit})</span>
        </span>
      </div>
    </div>
  );
}

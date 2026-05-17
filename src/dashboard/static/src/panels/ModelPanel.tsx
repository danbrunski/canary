import {
  definitionListClass,
  definitionRowClass,
  definitionTermClass,
  definitionValueClass,
  panelClass,
  panelHeaderClass,
  panelTitleClass,
  placeholderClass,
} from "../styles";
import type { AnomalyEvent, StatusResponse } from "../types";

interface ModelPanelProps {
  status: StatusResponse | null;
  events: AnomalyEvent[];
}

/** Model health panel (Task 5.12).
 *
 *  Pulls from two sources:
 *  * `/status.model_versions` for the live loaded versions (ModelManager
 *    polls these from disk every `daemon.model_poll_seconds`).
 *  * The recent anomaly stream for latency / score distribution — the
 *    LSTM result carries ``latency_ms``, and the IF emitter sets
 *    ``score``. Mean latency over the last N events is a decent proxy for
 *    "is the daemon keeping up?".
 *
 *  Once the daemon publishes deviation surfaces (4.E.9), this panel will
 *  also render those — for now it's model identity + latency + counters. */
export function ModelPanel({ status, events }: ModelPanelProps) {
  const versions = status?.model_versions ?? {};
  const counters = status?.emitter_counters ?? null;

  const lstmEvents = events.filter((e) => e.source === "lstm_autoencoder");
  const meanLstmLatency =
    lstmEvents.length === 0
      ? null
      : lstmEvents
          .map((e) => e.latency_ms ?? 0)
          .reduce((a, b) => a + b, 0) / lstmEvents.length;

  return (
    <section className={panelClass}>
      <header className={panelHeaderClass}>
        <h2 className={panelTitleClass}>Models</h2>
      </header>
      <dl className={definitionListClass}>
        {Object.entries(versions).length === 0 ? (
          <div className={placeholderClass}>no model artifacts loaded.</div>
        ) : (
          Object.entries(versions).map(([name, version]) => (
            <div key={name} className={definitionRowClass}>
              <dt className={definitionTermClass}>{name}</dt>
              <dd className={definitionValueClass}>{version ?? "no version"}</dd>
            </div>
          ))
        )}
        {meanLstmLatency !== null && (
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>lstm mean latency</dt>
            <dd className={definitionValueClass}>{meanLstmLatency.toFixed(1)} ms</dd>
          </div>
        )}
      </dl>
      {counters && (
        <div className="mt-3 flex gap-4 text-[0.78rem] text-fg-dim">
          <span title="anomaly events emitted to the logger + ws">
            emitted: <b className="text-fg">{counters.emitted}</b>
          </span>
          <span title="inference results that didn't cross the threshold">
            ok: <b className="text-fg">{counters.suppressed_not_anomaly}</b>
          </span>
          <span title="anomalies dropped within the dedupe window">
            deduped: <b className="text-fg">{counters.suppressed_dedupe}</b>
          </span>
        </div>
      )}
    </section>
  );
}

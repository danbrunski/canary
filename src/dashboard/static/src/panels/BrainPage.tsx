import { useCallback, useState } from "react";
import { TableScroll } from "../components/TableScroll";

import {
  getBrainJobs,
  getBrainStatus,
  getInvestigationCard,
  getTwinSession,
  queryMemory,
  submitBrainJob,
} from "../api/brain";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { ResourceGate } from "../components/ResourceGate";
import { Sparkline } from "../components/Sparkline";
import { useControlResource } from "../hooks/useControlResource";
import {
  buttonClass,
  cn,
  definitionListClass,
  definitionRowClass,
  definitionTermClass,
  definitionValueClass,
  inputClass,
  placeholderClass,
  type NoticeState,
} from "../styles";
import type {
  BrainJob,
  BrainStatus,
  LiveTwin,
  EvidenceView,
  InvestigationCardResponse,
  TwinEvaluation,
  TwinSessionResponse,
} from "../types/brain";
import { ProposalsView } from "./BrainProposals";
import { EcuApplicationModelPanel } from "./EcuApplicationModelPanel";

interface BrainPageProps {
  token: string;
}

const TOKEN_MESSAGE = "Enter the control token (Control panel) to view the second brain.";

/** Every distinct evidence view on a card, by id. */
export function cardEvidence(card: InvestigationCardResponse["card"]): Map<string, EvidenceView> {
  const views = new Map<string, EvidenceView>();
  const add = (view: EvidenceView) => views.set(view.id, view);
  card.what_happened.forEach(add);
  for (const hypothesis of card.hypotheses) {
    hypothesis.supporting.forEach(add);
    hypothesis.refuting.forEach(add);
  }
  return views;
}

/** The card's evidence as the server split it: what the ECM computed (the
 *  twin) and what the engine was measured doing. Never merged. */
export function explanationParts(response: InvestigationCardResponse): {
  ecu: EvidenceView[];
  engine: EvidenceView[];
} {
  const views = cardEvidence(response.card);
  const pick = (ids: string[]) =>
    ids.map((id) => views.get(id)).filter((v): v is EvidenceView => v !== undefined);
  return {
    ecu: pick(response.parts.ecu_computation),
    engine: pick(response.parts.engine_response),
  };
}

/** A job's state in words, with its named reason when it has one. */
export function jobStateText(job: BrainJob): string {
  return job.reason ? `${job.state} (${job.reason})` : job.state;
}

/** Counts by state, e.g. "unevaluable 174 · eligible 0". */
export function stateSummary(evaluation: TwinEvaluation): string {
  return Object.entries(evaluation.accounting.states)
    .map(([state, n]) => `${state} ${n}`)
    .join(" · ");
}

function Row({ term, value }: { term: string; value: string }) {
  return (
    <div className={definitionRowClass}>
      <dt className={definitionTermClass}>{term}</dt>
      <dd className={definitionValueClass}>{value}</dd>
    </div>
  );
}

function phi(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(5);
}

/** The live twin: what the ECM is commanding now against the decoded logic.
 *  An ECU-computation readout — never fuel delivery, never a fault. */
function LiveTwinView({ twin }: { twin: LiveTwin }) {
  if (!twin.enabled) {
    return (
      <Row term="Live twin" value={`${twin.result}${twin.reason ? ` — ${twin.reason}` : ""}`} />
    );
  }
  const latest = twin.latest_prediction;
  const states = twin.accounting?.states ?? {};
  const late = Object.values(twin.stream?.late_reads ?? {}).reduce((a, b) => a + b, 0);
  return (
    <>
      <Row
        term="Live twin"
        value={`${twin.result}${twin.reason ? ` (${twin.reason})` : ""} · vehicle ${
          twin.vehicle_state ?? "unknown"
        }${twin.replay ? " · replay" : ""}`}
      />
      <Row
        term="Latest command"
        value={
          latest
            ? `observed φ ${phi(latest.observed_phi)} · predicted φ ${phi(
                latest.predicted_phi,
              )} · difference ${phi(latest.difference_phi)} · residual ${
                latest.qualified_residual_phi === null
                  ? "not qualified"
                  : phi(latest.qualified_residual_phi)
              } (${latest.state}${latest.reason ? `: ${latest.reason}` : ""})`
            : "no prediction yet"
        }
      />
      <Row
        term="Live samples"
        value={`${Object.entries(states)
          .map(([state, n]) => `${state} ${n}`)
          .join(" · ")} · pending ${twin.stream?.pending ?? 0} · late ${late} · dropped ${
          twin.intake?.dropped ?? 0
        }`}
      />
      {twin.applicability && !twin.applicability.qualified ? (
        <Row term="Not qualified" value={twin.applicability.qualification_blockers.join(", ")} />
      ) : null}
    </>
  );
}

function StatusView({ status }: { status: BrainStatus }) {
  const { identity } = status;
  return (
    <dl className={definitionListClass}>
      <Row term="Program" value={identity.program_id ?? `unknown (${identity.program_status})`} />
      <Row term="Calibration" value={identity.calibration ?? "unknown"} />
      <Row
        term="Last identity change"
        value={`${status.last_change.kind} ${status.last_change.at}`}
      />
      <Row term="Twin tables" value={status.tables.source ?? `none (${status.tables.reason})`} />
      <Row term="RAM addresses" value={status.ram_addresses.status} />
      <Row
        term="Transient ECU state"
        value={`${status.transient.state} (${status.transient.reason})`}
      />
      <Row term="Memory" value={`${status.memory.current} current of ${status.memory.records}`} />
      {status.live_twin ? <LiveTwinView twin={status.live_twin} /> : null}
      <Row
        term="Jobs"
        value={
          Object.entries(status.jobs)
            .map(([state, n]) => `${state} ${n}`)
            .join(" · ") || "none"
        }
      />
      <p className="m-0 text-[0.8rem] text-fg-dim">{status.ram_addresses.detail}</p>
    </dl>
  );
}

function EvidenceList({ views, empty }: { views: EvidenceView[]; empty: string }) {
  if (views.length === 0) return <p className={placeholderClass}>{empty}</p>;
  return (
    <ul className="m-0 flex list-none flex-col gap-2 p-0">
      {views.map((view) => (
        <li key={view.id} className="border-b border-border pb-2 text-[0.84rem]">
          <div>
            <span className="font-num text-fg-dim">{view.id}</span> {view.tool} · {view.standing}
          </div>
          <div className="text-fg-dim">{view.subject}</div>
          {view.status === "unevaluable" ? (
            <div className="text-warn">
              unevaluable: {view.reason} — {view.detail}
            </div>
          ) : (
            <div>
              {view.values
                .map((v) => `${v.name} ${v.value} ${v.unit === "1" ? "" : v.unit}`.trim())
                .join(" · ")}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function CardView({ response }: { response: InvestigationCardResponse }) {
  const { card } = response;
  const { ecu, engine } = explanationParts(response);
  return (
    <div className="flex flex-col gap-3 text-[0.86rem]">
      <div>
        <strong>{card.title}</strong> · {card.status} · evidence {card.evidence_state}
      </div>
      <div className="text-fg-dim">{card.question}</div>
      <section>
        <h3 className={definitionTermClass}>ECU computation (twin prediction vs observed)</h3>
        <EvidenceList views={ecu} empty="no twin evidence cited" />
      </section>
      <section>
        <h3 className={definitionTermClass}>Measured engine response</h3>
        <EvidenceList views={engine} empty="no measured-response evidence cited" />
      </section>
      <section>
        <h3 className={definitionTermClass}>Hypotheses</h3>
        <ul className="m-0 list-none p-0">
          {card.hypotheses.map((h) => (
            <li key={h.label}>
              {h.label} — {h.status}
              {h.conflicting ? " (conflicting evidence)" : ""}
            </li>
          ))}
        </ul>
      </section>
      <section>
        <h3 className={definitionTermClass}>Missing inputs / unknowns</h3>
        {card.unknowns.length ? (
          <ul className="m-0 list-none p-0">
            {card.unknowns.map((u) => (
              <li key={u}>{u}</li>
            ))}
          </ul>
        ) : (
          <p className={placeholderClass}>none recorded</p>
        )}
      </section>
      <section>
        <h3 className={definitionTermClass}>Next measurement</h3>
        {card.next_measurements.length ? (
          <ul className="m-0 list-none p-0">
            {card.next_measurements.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        ) : (
          <p className={placeholderClass}>none proposed</p>
        )}
      </section>
      {card.narrative && (
        <p className="m-0 text-[0.8rem] text-fg-faint">
          {card.narrative_notice} {card.narrative}
        </p>
      )}
    </div>
  );
}

function TwinView({ response }: { response: TwinSessionResponse }) {
  if (!response.result) {
    return (
      <p className={placeholderClass}>
        {response.session}: {response.state}
        {response.job?.reason ? ` (${response.job.reason})` : ""}
      </p>
    );
  }
  const observed = (e: TwinEvaluation) =>
    e.trace.map((p) => p.observed_phi).filter((v): v is number => v !== null);
  return (
    <div className="flex flex-col gap-3 text-[0.86rem]">
      {response.currency && <div className="text-fg-dim">{response.currency.program_detail}</div>}
      {response.result.refusals.map((r) => (
        <div key={r.id} className="text-warn">
          slot {r.slot} refused: {r.reason} — {r.detail}
        </div>
      ))}
      {response.result.evaluations.map((evaluation) => {
        const predicted = evaluation.trace.filter((p) => p.predicted_phi !== null);
        return (
          <section key={evaluation.slot} className="flex flex-col gap-1">
            <div>
              slot {evaluation.slot} · {evaluation.accounting.candidates} command samples ·{" "}
              {stateSummary(evaluation)}
            </div>
            {evaluation.qualification_blockers.length > 0 && (
              <div className="text-warn">
                qualification blocked: {evaluation.qualification_blockers.join(", ")}
              </div>
            )}
            <div className="flex items-center gap-2">
              <span className={definitionTermClass}>observed φ</span>
              <Sparkline values={observed(evaluation)} width={240} />
              <span className="text-fg-dim">
                {predicted.length} with a prediction
                {evaluation.trace_omitted ? ` · ${evaluation.trace_omitted} not shown` : ""}
              </span>
            </div>
            {predicted.length > 0 && (
              <TableScroll>
                <table className="text-[0.8rem]">
                  <thead>
                    <tr>
                      <th className="text-left">time</th>
                      <th>observed φ</th>
                      <th>predicted φ</th>
                      <th>state</th>
                    </tr>
                  </thead>
                  <tbody>
                    {predicted.slice(0, 20).map((p) => (
                      <tr key={p.t}>
                        <td className="font-num">{p.t}</td>
                        <td className="font-num">{p.observed_phi ?? "cut"}</td>
                        <td className="font-num">{p.predicted_phi}</td>
                        <td>{p.reason ? `${p.state} (${p.reason})` : p.state}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableScroll>
            )}
            <div>
              <span className={definitionTermClass}>Missing inputs</span>{" "}
              {evaluation.missing_inputs.length ? evaluation.missing_inputs.join(", ") : "none"}
            </div>
            {evaluation.capture_requirement && (
              <div>
                <span className={definitionTermClass}>Next measurement</span>{" "}
                {evaluation.capture_requirement.message}
              </div>
            )}
            <div className="text-fg-faint">evidence {evaluation.evidence_id}</div>
          </section>
        );
      })}
    </div>
  );
}

/** The second brain as the phone sees it: the Pi's identity and recovery
 *  standing, its idle-time jobs, memory search, an investigation card with
 *  ECU computation and measured engine response kept apart, and a twin
 *  session evaluation. Every number comes from a minted record. */
export function BrainPage({ token }: BrainPageProps) {
  const enabled = Boolean(token);
  const status = useControlResource(
    useCallback((signal: AbortSignal) => getBrainStatus(token, signal), [token]),
    { enabled, intervalMs: 15000 },
  );
  const jobs = useControlResource(
    useCallback((signal: AbortSignal) => getBrainJobs(token, signal), [token]),
    { enabled, intervalMs: 5000 },
  );
  const [text, setText] = useState("");
  const [search, setSearch] = useState("");
  const memory = useControlResource(
    useCallback(
      (signal: AbortSignal) => queryMemory(token, { text: search, kind: "investigation" }, signal),
      [token, search],
    ),
    { enabled, intervalMs: null },
  );
  const [investigation, setInvestigation] = useState<string | null>(null);
  const card = useControlResource(
    useCallback(
      (signal: AbortSignal) => getInvestigationCard(token, investigation ?? "", signal),
      [token, investigation],
    ),
    { enabled: enabled && investigation !== null, intervalMs: null },
  );
  const [session, setSession] = useState<string | null>(null);
  const twin = useControlResource(
    useCallback(
      (signal: AbortSignal) => getTwinSession(token, session ?? "", signal),
      [token, session],
    ),
    { enabled: enabled && session !== null, intervalMs: null },
  );
  const [queueSession, setQueueSession] = useState("");
  const [notice, setNotice] = useState<NoticeState | null>(null);

  const queue = async (kind: BrainJob["kind"], params: Record<string, unknown> = {}) => {
    setNotice(null);
    try {
      const { job, created } = await submitBrainJob(token, kind, params);
      setNotice({
        kind: "ok",
        text: created ? `queued ${job.job_id}` : `already ${jobStateText(job)}`,
      });
      jobs.refresh();
    } catch (err) {
      setNotice({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    }
  };

  return (
    <>
      <Panel title="Second brain">
        <ResourceGate
          enabled={enabled}
          enabledMessage={TOKEN_MESSAGE}
          loading={status.loading}
          error={status.error}
          data={status.data}
        >
          {(data) => <StatusView status={data} />}
        </ResourceGate>
      </Panel>
      <Panel title="Next measurement (live)">
        <ResourceGate
          enabled={enabled}
          enabledMessage={TOKEN_MESSAGE}
          loading={status.loading}
          error={status.error}
          data={status.data}
        >
          {(data) =>
            data.live_proposals ? (
              <ProposalsView block={data.live_proposals} />
            ) : (
              <p className={placeholderClass}>this daemon does not publish proposals</p>
            )
          }
        </ResourceGate>
      </Panel>
      <Panel title="Brain jobs" count={jobs.data ? `${jobs.data.jobs.length} jobs` : undefined}>
        {enabled && (
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <input
              className={inputClass}
              placeholder="session id"
              aria-label="session to evaluate"
              value={queueSession}
              onChange={(e) => setQueueSession(e.target.value)}
            />
            <button
              type="button"
              className={buttonClass}
              disabled={!queueSession.trim()}
              onClick={() =>
                void queue("twin_session_evaluation", { session: queueSession.trim() })
              }
            >
              Evaluate session
            </button>
            <button
              type="button"
              className={buttonClass}
              onClick={() => void queue("memory_consolidation")}
            >
              Consolidate memory
            </button>
            <Notice notice={notice} />
          </div>
        )}
        <ResourceGate
          enabled={enabled}
          enabledMessage={TOKEN_MESSAGE}
          loading={jobs.loading}
          error={jobs.error}
          data={jobs.data}
        >
          {(data) =>
            data.jobs.length === 0 ? (
              <p className={placeholderClass}>no jobs; they run when collection is inactive</p>
            ) : (
              <ul className="m-0 flex list-none flex-col gap-1 p-0 text-[0.84rem]">
                {[...data.jobs].reverse().map((job) => {
                  const target = typeof job.params.session === "string" ? job.params.session : null;
                  return (
                    <li key={job.job_id} className="flex flex-wrap items-baseline gap-2">
                      <span className="font-num text-fg-dim">{job.job_id}</span>
                      <span>{job.kind}</span>
                      {target && (
                        <button
                          type="button"
                          className="text-accent underline"
                          onClick={() => setSession(target)}
                        >
                          {target}
                        </button>
                      )}
                      <span className={cn(job.state === "failed" && "text-bad")}>
                        {jobStateText(job)}
                      </span>
                      {job.detail && <span className="text-fg-faint">{job.detail}</span>}
                    </li>
                  );
                })}
              </ul>
            )
          }
        </ResourceGate>
      </Panel>
      <Panel title="Investigations">
        <form
          className="mb-3 flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setSearch(text);
          }}
        >
          <input
            className={inputClass}
            placeholder="search memory"
            aria-label="search memory"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <button type="submit" className={buttonClass} disabled={!enabled}>
            Search
          </button>
        </form>
        <ResourceGate
          enabled={enabled}
          enabledMessage={TOKEN_MESSAGE}
          loading={memory.loading}
          error={memory.error}
          data={memory.data}
        >
          {(data) =>
            data.records.length === 0 ? (
              <p className={placeholderClass}>no investigation matches</p>
            ) : (
              <ul className="m-0 flex list-none flex-col gap-1 p-0 text-[0.84rem]">
                {data.records.map((row) => (
                  <li key={row.record.id}>
                    <button
                      type="button"
                      className="text-accent underline"
                      onClick={() => setInvestigation(row.record.id)}
                    >
                      {row.record.title ?? row.record.id}
                    </button>{" "}
                    <span className="text-fg-faint">{row.record.status}</span>
                  </li>
                ))}
              </ul>
            )
          }
        </ResourceGate>
      </Panel>
      {investigation && (
        <Panel title="Investigation card">
          <ResourceGate loading={card.loading} error={card.error} data={card.data}>
            {(data) => <CardView response={data} />}
          </ResourceGate>
        </Panel>
      )}
      {session && (
        <Panel title={`Twin evaluation · ${session}`}>
          <ResourceGate loading={twin.loading} error={twin.error} data={twin.data}>
            {(data) => <TwinView response={data} />}
          </ResourceGate>
        </Panel>
      )}
      <EcuApplicationModelPanel token={token} />
    </>
  );
}

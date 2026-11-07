import { useCallback } from "react";

import { getApplicationModel } from "../api/ecuApplication";
import { Panel } from "../components/Panel";
import { ResourceGate } from "../components/ResourceGate";
import { useControlResource } from "../hooks/useControlResource";
import {
  cn,
  definitionListClass,
  definitionRowClass,
  definitionTermClass,
  definitionValueClass,
  placeholderClass,
} from "../styles";
import type {
  ApplicationModelReport,
  ApplicationOutput,
  FirmwareOverride,
  TwinRole,
} from "../types/ecuApplication";

/** The order a path is read in: what the twin computes, what it reads, what
 *  it takes on trust, what it leaves out, and what the calibration disables. */
export const TWIN_ROLES: readonly TwinRole[] = [
  "implemented",
  "observed",
  "assumed",
  "not_modelled",
  "calibration_dead",
];

const ROLE_LABEL: Record<TwinRole, string> = {
  implemented: "implemented",
  observed: "observed",
  assumed: "assumed",
  not_modelled: "not modelled",
  calibration_dead: "calibration-dead",
};

/** Node counts by twin role, in reading order, roles with none omitted. */
export function roleCounts(output: ApplicationOutput): { role: TwinRole; nodes: string[] }[] {
  return TWIN_ROLES.map((role) => ({ role, nodes: output.by_role[role] ?? [] })).filter(
    (row) => row.nodes.length > 0,
  );
}

/** "qualified" only with no blockers; otherwise the status and what holds it. */
export function outputStatusText(output: ApplicationOutput): string {
  if (output.blockers.length === 0) return output.status;
  return `${output.status} — ${output.blockers.map((b) => b.name).join(", ")}`;
}

const QUALIFICATION_CLASS: Record<FirmwareOverride["qualification"], string> = {
  traced: "text-fg-dim",
  fixture: "text-warn",
  vehicle: "text-ok",
};

/** Firmware overrides (E05.3): where the ECM replaces or reshapes a command,
 *  and how far each reading has been checked. */
function OverridesView({ overrides }: { overrides: readonly FirmwareOverride[] }) {
  return (
    <section>
      <h3 className={definitionTermClass}>Firmware overrides ({overrides.length})</h3>
      {overrides.length === 0 ? (
        <p className={placeholderClass}>none recorded</p>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {overrides.map((o) => (
            <li key={o.id}>
              <div>
                <strong>{o.id}</strong> <span className="text-fg-dim">{o.regime}</span> ·{" "}
                <span className={QUALIFICATION_CLASS[o.qualification]}>{o.qualification}</span>
                {o.routine !== null && <span className="font-num text-fg-dim"> {o.routine}</span>}
              </div>
              <div>
                when {o.condition}: {o.effect}
              </div>
              {o.gaps.length > 0 && (
                <div className="text-fg-faint">
                  {o.gaps.length} gap{o.gaps.length === 1 ? "" : "s"}; verify: {o.verify_with}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Row({ term, value }: { term: string; value: string }) {
  return (
    <div className={definitionRowClass}>
      <dt className={definitionTermClass}>{term}</dt>
      <dd className={definitionValueClass}>{value}</dd>
    </div>
  );
}

function OutputView({ output }: { output: ApplicationOutput }) {
  return (
    <section className="flex flex-col gap-1">
      <div>
        <strong>{output.id}</strong> <span className="font-num text-fg-dim">{output.ram}</span> ·{" "}
        {output.units} ·{" "}
        <span className={cn(output.status === "qualified" ? "text-ok" : "text-warn")}>
          {outputStatusText(output)}
        </span>
      </div>
      {output.twin && (
        <div className="text-fg-dim">
          twin {output.twin.definition} predicts {output.twin.predicts}
        </div>
      )}
      <ul className="m-0 list-none p-0">
        {roleCounts(output).map(({ role, nodes }) => (
          <li key={role}>
            <span className={definitionTermClass}>
              {ROLE_LABEL[role]} {nodes.length}
            </span>{" "}
            <span className="text-fg-dim">{nodes.join(", ")}</span>
          </li>
        ))}
      </ul>
      {output.blockers.map((b) => (
        <div key={b.name} className="text-warn">
          {b.name} ({b.nodes.length} nodes): {b.closes_with}
        </div>
      ))}
      {output.regions.length > 0 && (
        <ul className="m-0 list-none p-0">
          {output.regions.map((r) => (
            <li key={r.id}>
              <span className={cn(r.status === "unevaluable" ? "text-bad" : "text-warn")}>
                {r.status}
              </span>{" "}
              {r.id}: <span className="text-fg-dim">{r.when}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ReportView({ report }: { report: ApplicationModelReport }) {
  const { model, program, image } = report;
  return (
    <div className="flex flex-col gap-3 text-[0.86rem]">
      <dl className={definitionListClass}>
        <Row term="Model" value={`${model.name} v${model.model_version}`} />
        <Row term="Program" value={`${program.id} · patch ${program.patch}`} />
        <Row
          term="Running image"
          value={
            image === null
              ? "not recorded in the flash history"
              : image.known
                ? `${image.revision ?? "?"} (${image.role ?? "?"})`
                : `${image.sha256.slice(0, 12)}… not checked against this program`
          }
        />
      </dl>
      {report.outputs.map((output) => (
        <OutputView key={output.id} output={output} />
      ))}
      {report.overrides !== undefined && <OverridesView overrides={report.overrides} />}
      <section>
        <h3 className={definitionTermClass}>Uncovered logic ({report.uncovered.length})</h3>
        {report.uncovered.length === 0 ? (
          <p className={placeholderClass}>none recorded</p>
        ) : (
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {report.uncovered.map((u) => (
              <li key={u.id}>
                <div>
                  <strong>{u.id}</strong> <span className="text-fg-dim">{u.where}</span>
                </div>
                <div>blocks {u.blocks}</div>
                <div className="text-fg-faint">next: {u.next_evidence}</div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

/** What the decoded ECM logic covers (E05.1): each output qualified or not
 *  and why, the traced path by twin role, and the logic still uncovered. */
export function EcuApplicationModelPanel({ token }: { token: string }) {
  const enabled = Boolean(token);
  const report = useControlResource(
    useCallback((signal: AbortSignal) => getApplicationModel(token, signal), [token]),
    { enabled, intervalMs: null },
  );
  return (
    <Panel title="ECU application model">
      <ResourceGate
        enabled={enabled}
        tokenPurpose="view the ECU application model"
        loading={report.loading}
        error={report.error}
        data={report.data}
      >
        {(data) => <ReportView report={data} />}
      </ResourceGate>
    </Panel>
  );
}

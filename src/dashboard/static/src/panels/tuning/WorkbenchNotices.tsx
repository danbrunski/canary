import type { ReactNode } from "react";

import type {
  Cell,
  Conditions,
  GateReport,
  Refusal,
  UnavailableKind,
  UnavailableSession,
} from "../../lib/workbenchApi";
import { gateSentence } from "../../lib/workbenchApi";
import { cn } from "../../styles";

/** A refused request, in the words its kind demands. */
export function RefusalNotice({ refusal }: { refusal: Refusal | null }) {
  if (!refusal) return null;
  if (refusal.kind === "deferred") {
    return (
      <div role="status" className="rounded border border-warn/60 p-3 text-sm text-warn">
        <p className="m-0">
          Deferred while <code>{refusal.profile ?? "a capture"}</code> is recording — analysis would
          compete with the collector for memory.
        </p>
        {refusal.laptopPath && (
          <p className="m-0 mt-1 text-xs text-fg-dim">
            Laptop path: <code className="break-all">{refusal.laptopPath}</code>
          </p>
        )}
      </div>
    );
  }
  const tone = refusal.kind === "busy" ? "text-warn" : "text-bad";
  return (
    <p role="status" className={cn("m-0 text-sm", tone)}>
      {refusal.message}
    </p>
  );
}

/** The two kinds of empty, never merged: "gates removed everything" is an
 *  answer about the drive; "never captured" is not an answer at all. */
export function EmptyResult({
  kind,
  gates,
  unavailable,
}: {
  kind: UnavailableKind;
  gates: GateReport | null;
  unavailable: UnavailableSession[];
}) {
  if (kind === "gates_removed_everything") {
    return (
      <p role="status" data-empty="gates" className="m-0 text-sm text-warn">
        {gateSentence(gates)}
      </p>
    );
  }
  if (kind === "not_captured") {
    const missing = [...new Set(unavailable.flatMap((item) => item.missing))];
    return (
      <p role="status" data-empty="not-captured" className="m-0 text-sm text-bad">
        This capture's profile does not poll {missing.length ? missing.join(", ") : "the inputs"}
      </p>
    );
  }
  if (kind === "over_budget") {
    return (
      <p role="status" className="m-0 text-sm text-bad">
        Every chosen session is over the analysis row budget — analyse on the laptop.
      </p>
    );
  }
  return (
    <p role="status" className="m-0 text-sm text-fg-dim">
      {unavailable[0]?.reason ?? "No result."}
    </p>
  );
}

export function UnavailableList({ items }: { items: UnavailableSession[] }) {
  if (!items.length) return null;
  return (
    <details className="text-xs text-fg-dim">
      <summary>{items.length} session(s) did not contribute</summary>
      <ul className="mt-1 space-y-1">
        {items.map((item) => (
          <li key={`${item.session_id}-${item.kind}`}>
            <code>{item.session_id}</code> — {item.kind.replace(/_/g, " ")}: {item.reason}
          </li>
        ))}
      </ul>
    </details>
  );
}

/** W04.12: sessions pooled, revision(s), barometric range, gates and rows
 *  each removed. Rendered under every figure. */
export function ConditionsBlock({
  conditions,
  extra,
}: {
  conditions: Conditions | null | undefined;
  extra?: ReactNode;
}) {
  if (!conditions) return null;
  const revisions = Object.entries(conditions.revisions);
  const baro = conditions.baro;
  const boundaries = Object.entries(conditions.revision_boundaries ?? {});
  return (
    <dl
      aria-label="Conditions"
      className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs text-fg-dim"
    >
      <dt>Sessions</dt>
      <dd className="m-0">
        {conditions.n_sessions} pooled
        {conditions.sessions.length > 0 && (
          <span className="text-fg-faint"> ({conditions.sessions.join(", ")})</span>
        )}
      </dd>
      <dt>Revision</dt>
      <dd className={cn("m-0", conditions.mixed_revisions && "text-warn")}>
        {revisions.length === 0
          ? "none"
          : revisions.map(([rev, names]) => `${rev} (${names.length})`).join(", ")}
        {conditions.mixed_revisions && " — mixes calibrations"}
      </dd>
      {boundaries.length > 0 && (
        <>
          <dt>Flash inside</dt>
          <dd className="m-0 text-warn">
            {boundaries
              .map(
                ([session, crossed]) => `${session} → ${crossed.map((b) => b.revision).join(", ")}`,
              )
              .join("; ")}
          </dd>
        </>
      )}
      <dt>Barometric</dt>
      <dd className="m-0">
        {baro.min_kpa === null
          ? `not captured (${baro.spec})`
          : `${baro.min_kpa.toFixed(1)}–${(baro.max_kpa ?? baro.min_kpa).toFixed(1)} kPa`}
        {baro.sessions_without.length > 0 && baro.min_kpa !== null && (
          <span className="text-warn">
            {" "}
            · {baro.sessions_without.length} session(s) without baro
          </span>
        )}
      </dd>
      <dt>Gates</dt>
      <dd className="m-0">{conditions.gates ? gateSentence(conditions.gates) : "none applied"}</dd>
      {extra}
    </dl>
  );
}

/** Colour ramp for a cell value against the grid's own extent. */
function ramp(t: number): string {
  const clamped = Math.max(0, Math.min(1, t));
  const r = Math.round(40 + 215 * clamped);
  const g = Math.round(90 + 90 * (1 - Math.abs(clamped - 0.5) * 2));
  const b = Math.round(200 - 170 * clamped);
  return `rgb(${r}, ${g}, ${b})`;
}

/** An rpm x airflow grid as DOM cells (readable and testable without a
 *  canvas). Sparse cells are grey with their n, never coloured. */
export function CellGrid({
  cells,
  rpmEdges,
  airflowEdges,
  valueKey,
  unit,
  highlight,
  label,
}: {
  cells: Cell[];
  rpmEdges: number[];
  airflowEdges: number[];
  valueKey: string;
  unit: string;
  highlight?: (cell: Cell) => boolean;
  label: string;
}) {
  const byKey = new Map(cells.map((cell) => [`${cell.rpm_index}:${cell.airflow_index}`, cell]));
  const usedRpm = [...new Set(cells.map((c) => c.rpm_index))].sort((a, b) => b - a);
  const usedAir = [...new Set(cells.map((c) => c.airflow_index))].sort((a, b) => a - b);
  const values = cells
    .filter((c) => !c.sparse && typeof c[valueKey] === "number")
    .map((c) => c[valueKey] as number);
  const lo = values.length ? Math.min(...values) : 0;
  const hi = values.length ? Math.max(...values) : 1;
  const span = hi - lo || 1;
  if (!cells.length) return <p className="m-0 text-sm text-fg-dim">No cells.</p>;
  return (
    <div className="overflow-x-auto">
      <table aria-label={label} className="border-collapse text-[0.7rem]">
        <thead>
          <tr>
            <th className="px-1 text-left text-fg-faint">rpm \ airflow</th>
            {usedAir.map((a) => (
              <th key={a} className="px-1 font-normal text-fg-faint">
                {airflowEdges[a]}–{airflowEdges[a + 1]}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {usedRpm.map((r) => (
            <tr key={r}>
              <th className="px-1 text-left font-normal text-fg-faint">
                {rpmEdges[r]}–{rpmEdges[r + 1]}
              </th>
              {usedAir.map((a) => {
                const cell = byKey.get(`${r}:${a}`);
                if (!cell) return <td key={a} className="border border-border-soft" />;
                const value = cell[valueKey];
                const numeric = typeof value === "number" ? value : null;
                const flagged = highlight?.(cell) ?? false;
                return (
                  <td
                    key={a}
                    data-sparse={cell.sparse ? "true" : undefined}
                    data-flagged={flagged ? "true" : undefined}
                    title={`n=${cell.n}${cell.sparse ? " (below minimum n)" : ""}`}
                    className={cn(
                      "min-w-12 border px-1 text-center font-num",
                      flagged ? "border-bad border-2" : "border-border-soft",
                      cell.sparse && "bg-elev-2 text-fg-faint",
                    )}
                    style={
                      cell.sparse || numeric === null
                        ? undefined
                        : { background: ramp((numeric - lo) / span), color: "#0b0f14" }
                    }
                  >
                    {numeric === null ? "—" : numeric.toFixed(1)}
                    <span className="block text-[0.6rem] opacity-70">n {cell.n}</span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="m-0 mt-1 text-[0.65rem] text-fg-faint">
        Values in {unit}; grey cells are below the minimum sample count and are not coloured.
      </p>
    </div>
  );
}

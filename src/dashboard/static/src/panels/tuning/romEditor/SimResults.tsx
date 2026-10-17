import { TableScroll } from "../../../components/TableScroll";
import { coarseVerdict, formatCount, PRESET_DRAWS } from "../../../lib/romSimSize";
import { g, pct } from "../../../lib/romSimView";
import { buttonClass } from "../../../styles";
import type { RomSimResult, SimRunContext } from "../../../types/romEditor";
import { SimCaveats } from "./SimCaveats";
import { SimMaps } from "./SimMaps";

function Hash({ children }: { children: string | null | undefined }) {
  return <code className="break-all font-num text-xs">{children ?? "not stated"}</code>;
}

function counts(map: Record<string, number> | undefined): string {
  const entries = Object.entries(map ?? {}).sort(([a], [b]) => a.localeCompare(b));
  return entries.length === 0 ? "none" : entries.map(([k, v]) => `${k} ${g(v)}`).join(", ");
}

/** The design summary and provenance (below the maps): what was run, with the
 *  hashes that identify it, and the result as a file for the laptop. */
function Provenance({
  result,
  run,
  onDownload,
}: {
  result: RomSimResult;
  run: SimRunContext;
  onDownload: () => void;
}) {
  const { design, provenance, cells: table } = result;
  const environment = design.grid?.environment ?? {};
  const statuses = [
    "changed",
    "unchanged",
    "unsupported",
    "partly_supported",
    "no_points",
    "not_reached",
  ];
  return (
    <section aria-label="Design and provenance" className="space-y-3">
      <h3 className="text-base font-semibold">Design and provenance</h3>
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 break-words text-sm">
        <dt className="text-fg-dim">design sha256</dt>
        <dd>
          <Hash>{design.sha256}</Hash>
        </dd>
        <dt className="text-fg-dim">kind, base revision</dt>
        <dd>
          {design.kind}, {design.base_revision}
        </dd>
        <dt className="text-fg-dim">points, weight</dt>
        <dd>
          {design.points} points, weight {g(design.weight)} ({design.weight_basis})
        </dd>
        <dt className="text-fg-dim">placement</dt>
        <dd>{counts(design.placement)}</dd>
        <dt className="text-fg-dim">plant states</dt>
        <dd>{counts(design.plant_states)}</dd>
        <dt className="text-fg-dim">weight by regime</dt>
        <dd>{counts(design.weight_by_regime)}</dd>
        <dt className="text-fg-dim">throttle, battery sources</dt>
        <dd>
          {counts(design.points_by_throttle_source)}; {counts(design.points_by_battery_source)}
        </dd>
        <dt className="text-fg-dim">fill-byte interval width</dt>
        <dd>
          {design.fill_byte_width
            ? `median ${g(design.fill_byte_width.median)}, max ${design.fill_byte_width.max} counts`
            : "—"}
        </dd>
        <dt className="text-fg-dim">rpm edges</dt>
        <dd className="font-num text-xs">{design.grid?.rpm_edges.join(", ") ?? "—"}</dd>
        <dt className="text-fg-dim">load edges</dt>
        <dd className="font-num text-xs">
          {design.grid?.load_edges.join(", ") ?? "—"} {design.grid?.load_unit}
        </dd>
        <dt className="text-fg-dim">load basis</dt>
        <dd className="font-num text-xs">{design.grid?.load_basis ?? "—"}</dd>
        <dt className="text-fg-dim">environment as run</dt>
        <dd className="font-num text-xs">
          {Object.entries(environment)
            .map(([k, v]) => `${k} ${v}`)
            .join(", ") || "—"}
        </dd>
        <dt className="text-fg-dim">draws, seed</dt>
        <dd className="font-num">
          {table.draws} draws, seed {provenance.seed ?? "not stated"}
        </dd>
        <dt className="text-fg-dim">candidate events</dt>
        <dd>{(provenance.candidate_events ?? []).join(", ") || "none"}</dd>
        <dt className="text-fg-dim">plant version</dt>
        <dd className="font-num text-xs">{provenance.plant_version ?? "not stated"}</dd>
        <dt className="text-fg-dim">base image</dt>
        <dd>
          <Hash>{table.base_image_sha256}</Hash>
        </dd>
        <dt className="text-fg-dim">candidate image</dt>
        <dd>
          <Hash>{table.candidate_image_sha256}</Hash>
        </dd>
        <dt className="text-fg-dim">spec sha256</dt>
        <dd>
          <Hash>{result.spec_sha256}</Hash>
        </dd>
        <dt className="text-fg-dim">uncertainty sha256</dt>
        <dd>
          <Hash>{provenance.uncertainty_sha256}</Hash>
        </dd>
        <dt className="text-fg-dim">contract sha256</dt>
        <dd>
          <Hash>{provenance.contract_sha256}</Hash>
        </dd>
        <dt className="text-fg-dim">application model sha256</dt>
        <dd>
          <Hash>{provenance.application_model_sha256}</Hash>
        </dd>
        <dt className="text-fg-dim">result digest</dt>
        <dd>
          <Hash>{table.result_digest}</Hash>
        </dd>
        <dt className="text-fg-dim">quantiles</dt>
        <dd>
          {table.quantiles.join(", ")} ({table.quantile_method}, weighted); {table.weight_basis}
        </dd>
        <dt className="text-fg-dim">cell membership</dt>
        <dd className="text-xs">{table.membership_rule}</dd>
        <dt className="text-fg-dim">inert parameters</dt>
        <dd className="text-xs">{result.inert_parameters.join(", ") || "none"}</dd>
        <dt className="text-fg-dim">took</dt>
        <dd>{run.elapsedS} s</dd>
      </dl>

      <div className="space-y-1">
        <h4 className="text-sm font-semibold">Status counts (cells per output)</h4>
        <TableScroll className="rounded-lg border border-border">
          <table aria-label="Status counts" className="w-full min-w-[32rem] text-left text-xs">
            <thead className="text-[0.7rem] uppercase tracking-[0.08em] text-fg-faint">
              <tr>
                <th className="px-2 py-1.5">Output</th>
                {statuses.map((status) => (
                  <th key={status} className="px-2 py-1.5">
                    {status.replace("_", " ")}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.outputs.map((name) => (
                <tr key={name} className="border-t border-border font-num">
                  <td className="px-2 py-1">{name}</td>
                  {statuses.map((status) => (
                    <td key={status} className="px-2 py-1">
                      {table.status_counts[name]?.[status] ?? 0}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
        <p className="text-xs text-fg-faint">
          Reach per output:{" "}
          {Object.entries(table.reach)
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([name, reach]) => `${name} ${reach.status}`)
            .join(", ")}
          . Supported weight:{" "}
          {Object.entries(result.coverage)
            .map(([name, share]) => `${name} ${pct(share)}`)
            .join(", ") || "not stated"}
          .
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button type="button" className={buttonClass} onClick={onDownload}>
          Download result JSON
        </button>
        <span className="text-xs text-fg-dim">
          The response as a file, for the laptop. It holds the cell table, not an image: there is no
          image to download.
        </span>
      </div>
    </section>
  );
}

/** Cells of the first output with no visited design point. */
function noPointsCells(table: RomSimResult["cells"]): { none: number; all: number } | null {
  const name = table.outputs[0];
  const counts = name ? table.status_counts[name] : undefined;
  if (!counts) return null;
  return {
    none: counts.no_points ?? 0,
    all: Object.values(counts).reduce((total, count) => total + count, 0),
  };
}

/** The banner of a run whose draws or points are below the Fine preset's (W09.9),
 *  judged from the result's own numbers, so a hand-set low run is flagged like
 *  a clicked preset. The wording is E08.3's: a few draws give only a coarse
 *  per-cell spread. */
function CoarseBanner({ result, run }: { result: RomSimResult; run: SimRunContext }) {
  const draws = result.cells.draws;
  const points = result.design.points;
  const fine = run.fineReference ?? { points: null, draws: PRESET_DRAWS.fine };
  const verdict = coarseVerdict(draws, points, fine);
  if (!verdict.coarse) return null;
  const cells = noPointsCells(result.cells);
  return (
    <section
      role="note"
      aria-label="Coarse run"
      className="space-y-1 rounded-lg border border-warn/70 bg-warn/10 p-3 text-sm"
    >
      <h3 className="text-base font-semibold text-warn">Coarse run</h3>
      <p>
        <span className="font-num">
          {formatCount(draws)} draws × {formatCount(points)} points
        </span>{" "}
        ({verdict.reasons.join("; ")}). This is a quick check, not a thorough run.
      </p>
      {verdict.fewPoints && (
        <p>
          Cells with no visited point read <code className="font-num">no_points</code>
          {cells ? ` (${formatCount(cells.none)} of ${formatCount(cells.all)} cells here)` : ""}:
          nothing was simulated there, which is not <code className="font-num">unchanged</code>.
        </p>
      )}
      <p>
        Quantiles over few draws are noisy: each cell&apos;s p05 / p50 / p95 are taken over its
        (point, draw) pairs, and E08.3 recorded that three draws give only a coarse per-cell spread
        (it reads cell quantiles from a run of {formatCount(PRESET_DRAWS.coarse)}). The Fine preset
        runs {formatCount(fine.draws)} draws
        {fine.points !== null ? ` on ${formatCount(fine.points)} points` : ""}.
      </p>
    </section>
  );
}

/** A simulation result (W09.4). The caveat block is the first thing in it, in
 *  the DOM as on screen, and the maps never render without it. */
export function SimResults({
  result,
  run,
  stale,
  onDownload,
}: {
  result: RomSimResult;
  run: SimRunContext;
  /** The pending edits have changed since this run. */
  stale: boolean;
  onDownload: () => void;
}) {
  return (
    <div role="region" aria-label="Simulation result" className="space-y-4">
      {stale && (
        <p role="status" className="text-sm text-warn">
          The pending edits have changed since this run. The result below is for the edits it lists.
        </p>
      )}
      <CoarseBanner result={result} run={run} />
      <SimCaveats result={result} run={run} />
      <SimMaps table={result.cells} />
      <Provenance result={result} run={run} onDownload={onDownload} />
    </div>
  );
}

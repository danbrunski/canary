import { TableScroll } from "../../../components/TableScroll";
import {
  claimLine,
  pointSummary,
  reasonCounts,
  stateText,
  timingGrid,
  timingMark,
  type MarkKind,
} from "../../../lib/romSimTiming";
import { formatCount } from "../../../lib/romSimSize";
import { cn } from "../../../styles";
import type { IgnitionTimingBlock } from "../../../types/romEditor";
import type { RomTableValues } from "../../../types/romLibrary";

const MARK_CLASS: Record<MarkKind, string> = {
  changed: "bg-accent-2/35 font-semibold",
  unchanged: "bg-elev-2/60 text-fg-faint",
  not_read: "bg-warn/20 text-warn",
  unevaluable: "bg-elev-2/60 text-fg-dim underline decoration-dotted",
};

const LEGEND: { kind: MarkKind; text: string; meaning: string }[] = [
  { kind: "changed", text: "-3", meaning: "timing changes by that many degrees BTDC" },
  { kind: "unchanged", text: "·", meaning: "no change (the cell is not in the result)" },
  {
    kind: "not_read",
    text: "NR",
    meaning: "the edited trim is not the one the stated HI DET latch makes the ECM read",
  },
  { kind: "unevaluable", text: "U", meaning: "no number can be given (see the reasons)" },
];

/** The ignition timing change of a simulate (E09.7): the ECU side only, the
 *  timing byte's chain with every term but the selected trim held equal. It
 *  shows the stated state, the change per cell of the trims' own grid, and every
 *  reason a cell or a point has no number. It claims nothing about torque or
 *  knock: those are E07.5's own refusals, printed as the block carries them. */
export function SimTiming({
  timing,
  tables,
}: {
  timing: IgnitionTimingBlock;
  /** The base's tables, for the trims' axis labels. */
  tables: readonly RomTableValues[];
}) {
  const grid = timingGrid(timing, tables);
  const reasons = reasonCounts(timing);
  const points = pointSummary(timing.points);
  const summary = timing.summary;
  const stated = timing.state;
  return (
    <section aria-label="Ignition timing" className="space-y-3">
      <h3 className="text-base font-semibold">Ignition timing (ECU side)</h3>
      <p className="text-sm text-fg-dim">
        The change in the timing byte&apos;s chain from the edit, in degrees BTDC (one trim count is
        one degree), with every other term held equal on both ROMs. It is no torque claim and no
        knock claim.
      </p>
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 break-words text-sm">
        <dt className="text-fg-dim">status</dt>
        <dd>
          <code className="font-num text-xs" data-timing-status={timing.status}>
            {timing.status}
          </code>
          {timing.status === "unevaluable" && " (no number for this edit; reasons below)"}
        </dd>
        <dt className="text-fg-dim">stated by you</dt>
        <dd>{stated ? stateText(stated) : "not stated"}</dd>
        <dt className="text-fg-dim">trims edited</dt>
        <dd>{timing.trims_changed ? "yes" : "no"}</dd>
        {timing.cells_at && (
          <>
            <dt className="text-fg-dim">cells read at</dt>
            <dd>
              coolant <span className="font-num">{timing.cells_at.coolant_c} °C</span> (your stated
              coolant), pedal flag bit 0 {timing.cells_at.pedal_flag ? "set" : "clear"}
            </dd>
          </>
        )}
        {summary && (
          <>
            <dt className="text-fg-dim">trim cells</dt>
            <dd className="font-num text-xs">
              {summary.cells} in the grid: {summary.changed} changed,{" "}
              {summary.changed_trim_not_read} edited but not read, {summary.unevaluable} unevaluable
            </dd>
          </>
        )}
        <dt className="text-fg-dim">design points</dt>
        <dd className="font-num text-xs">
          {points.pairs === 0
            ? "none"
            : `${formatCount(points.pairs)} (point, draw) pairs on ${formatCount(points.points)} points: ${
                Object.entries(points.byStatus)
                  .sort(([a], [b]) => a.localeCompare(b))
                  .map(([status, n]) => `${status} ${formatCount(n)}`)
                  .join(", ") || "none"
              }${points.numeric > 0 ? `; ${formatCount(points.changed)} show a change` : ""}`}
        </dd>
        <dt className="text-fg-dim">torque</dt>
        <dd className="font-num text-xs">{claimLine(timing.torque)}</dd>
        <dt className="text-fg-dim">knock</dt>
        <dd className="font-num text-xs">{claimLine(timing.knock)}</dd>
      </dl>

      {grid && (
        <div className="space-y-1.5">
          <h4 className="text-sm font-semibold">Change per trim cell (degrees BTDC)</h4>
          {grid.mismatch && (
            <p role="note" className="text-xs text-warn">
              {grid.mismatch}
            </p>
          )}
          <TableScroll className="rounded-lg border border-border">
            <table
              aria-label="Ignition timing change per trim cell"
              className="min-w-[44rem] border-separate border-spacing-0.5 text-center font-num text-xs"
            >
              <caption className="sr-only">
                Timing change in degrees per cell of the ignition timing trims&apos; own grid: rows
                are {grid.labelsFrom === "trim axes" ? "rpm" : "rpm words"}, columns are the load
                input X in counts.
              </caption>
              <thead>
                <tr>
                  <th scope="col" className="p-1" />
                  {grid.colLabels.map((name, c) => (
                    <th key={c} scope="col" className="p-1 font-normal text-fg-dim">
                      {name}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {grid.cells.map((line, r) => (
                  <tr key={r}>
                    <th scope="row" className="p-1 text-right font-normal text-fg-dim">
                      {grid.rowLabels[r]}
                    </th>
                    {line.map((cell, c) => {
                      const mark = timingMark(cell);
                      return (
                        <td
                          key={c}
                          data-row={r}
                          data-col={c}
                          data-timing-mark={mark.kind}
                          title={mark.title}
                          aria-label={`Row ${r + 1}, column ${c + 1}: ${mark.text} (${mark.title})`}
                          className={cn("min-w-9 rounded p-1", MARK_CLASS[mark.kind])}
                        >
                          {mark.text}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
          <ul
            aria-label="Legend: ignition timing"
            className="flex flex-wrap gap-x-4 gap-y-1 text-xs"
          >
            {LEGEND.map((item) => (
              <li key={item.kind} data-mark={item.kind} className="flex items-center gap-1.5">
                <span
                  aria-hidden="true"
                  className={cn(
                    "inline-flex h-5 w-9 shrink-0 items-center justify-center rounded-sm border border-border font-num text-[0.65rem]",
                    MARK_CLASS[item.kind],
                  )}
                >
                  {item.text}
                </span>
                <span className="text-fg-dim">{item.meaning}</span>
              </li>
            ))}
          </ul>
          <p className="text-xs text-fg-faint">
            Rows are {grid.labelsFrom === "trim axes" ? "rpm" : "rpm words"}, columns the load input
            X in counts (the trims&apos; own axes). Each cell is read at its own node, with the same
            X on both ROMs.
          </p>
        </div>
      )}

      <div className="space-y-1">
        <h4 className="text-sm font-semibold">Reasons ({reasons.length})</h4>
        {reasons.length === 0 ? (
          <p className="text-sm text-fg-dim">none: every number above stands as it is.</p>
        ) : (
          <ul aria-label="Timing reasons" className="space-y-1.5 text-sm">
            {reasons.map((item) => (
              <li key={item.reason} data-reason={item.reason}>
                <code className="break-all font-num text-xs">{item.reason}</code>
                {(item.cells > 0 || item.pairs > 0) && (
                  <span className="text-xs text-fg-faint">
                    {" "}
                    (
                    {[
                      item.cells > 0
                        ? `${formatCount(item.cells)} trim ${item.cells === 1 ? "cell" : "cells"}`
                        : null,
                      item.pairs > 0 ? `${formatCount(item.pairs)} design pairs` : null,
                    ]
                      .filter(Boolean)
                      .join(", ")}
                    )
                  </span>
                )}
                <span className="block text-fg-dim">
                  {item.text ??
                    "This page has no sentence for this reason; the code is the daemon's."}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <details className="text-sm">
        <summary className="cursor-pointer text-fg-dim">
          What is held equal on both ROMs ({timing.conditions.length})
        </summary>
        <ul className="mt-1 list-inside list-disc space-y-0.5 text-xs">
          {timing.conditions.map((condition) => (
            <li key={condition}>{condition}</li>
          ))}
        </ul>
      </details>
    </section>
  );
}

import type { FrameResponse } from "../../types/workbench";

/** What a frame says about itself above its rows: revision and grid, a flash
 *  inside the log, channels never polled, the regime filter's outcome, the
 *  freshness bound, and the page controls when the frame is a capped page. */
export function FrameNotes({
  frame,
  onOffset,
}: {
  frame: FrameResponse;
  onOffset: (n: number) => void;
}) {
  const report = frame.regime_report;
  const end = frame.offset + frame.rows_returned;
  return (
    <div className="mb-2 space-y-1 text-xs text-fg-dim">
      <p className="m-0">
        Revision {frame.revision.revision ?? "unknown"} ({frame.revision.source.replace(/_/g, " ")})
        {frame.profile && ` · profile ${frame.profile}`} · grid {frame.grid}
      </p>
      {frame.revision_boundaries.length > 0 && (
        <p role="alert" className="m-0 text-warn">
          A flash falls inside this log (
          {frame.revision_boundaries
            .map((b) => `${b.revision} at ${new Date(b.flashed_at).toLocaleString()}`)
            .join(", ")}
          ). The registry labels the whole log by its start, so figures across the marker mix two
          calibrations.
        </p>
      )}
      {frame.missing.map((item) => (
        <p key={item.spec} className="m-0 text-bad">
          {item.reason}
        </p>
      ))}
      {report.available === false && (
        <p className="m-0 text-bad">Regime filter unavailable: {report.reason}</p>
      )}
      <p className="m-0">
        {frame.freshness.dropped.toLocaleString()} of{" "}
        {frame.freshness.cells_with_any.toLocaleString()} cells dropped by the freshness bound
        (every channel must be sampled in the cell)
        {report.regime !== "all" &&
          report.available !== false &&
          ` · regime ${report.regime.replace(/_/g, " ")} kept ${report.after} of ${report.before}` +
            (report.unknown ? ` (${report.unknown} unclassifiable)` : "")}
      </p>
      {(frame.capped || frame.offset > 0) && (
        <p className="m-0 text-warn">
          Showing rows {frame.offset + 1}–{end} of {frame.rows_total.toLocaleString()} (cap{" "}
          {frame.row_cap.toLocaleString()} per page).{" "}
          {frame.offset > 0 && (
            <button
              type="button"
              className="underline"
              onClick={() => onOffset(Math.max(0, frame.offset - frame.row_cap))}
            >
              Previous
            </button>
          )}{" "}
          {frame.capped && (
            <button type="button" className="underline" onClick={() => onOffset(end)}>
              Next
            </button>
          )}
        </p>
      )}
    </div>
  );
}

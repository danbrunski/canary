import { useCallback, useMemo, useRef, useState } from "react";

import { TableScroll } from "../../components/TableScroll";
import {
  ROW_HEIGHT,
  TIME_KEY,
  VIEWPORT_ROWS,
  formatFrameValue,
  frameCsv,
  frameCsvName,
  sortedRowOrder,
  visibleRowWindow,
  type FrameSort,
} from "../../lib/frameTable";
import { buttonClass } from "../../styles";
import type { FrameResponse } from "../../types/workbench";
import { downloadText } from "../../util/browser";

/** A frame as a sortable table of every row, rendering only the window in
 *  view (rows are a fixed height; spacer rows stand in for the rest), with a
 *  CSV export of exactly the rows in the order shown. */
export function FrameTable({ frame }: { frame: FrameResponse }) {
  const [sort, setSort] = useState<FrameSort>({ key: TIME_KEY, dir: 1 });
  const [scrollTop, setScrollTop] = useState(0);
  const scroller = useRef<HTMLDivElement>(null);
  const specs = frame.columns.map((c) => c.spec);

  const order = useMemo(() => sortedRowOrder(frame, sort), [frame, sort]);

  const { first, last } = visibleRowWindow(scrollTop, order.length);
  const visible = order.slice(first, last);

  const exportCsv = useCallback(() => {
    downloadText(frameCsv(frame, order), frameCsvName(frame), "text/csv");
  }, [frame, order]);

  const toggle = (key: string) =>
    setSort((current) => ({ key, dir: current.key === key ? (current.dir === 1 ? -1 : 1) : 1 }));
  const arrow = (key: string) => (sort.key === key ? (sort.dir === 1 ? " ▲" : " ▼") : "");

  if (frame.rows_total === 0) {
    return <p className="m-0 text-sm text-fg-dim">No rows in this view.</p>;
  }
  return (
    <div>
      <div className="mb-1 flex justify-end">
        <button type="button" className={buttonClass} onClick={exportCsv}>
          Export CSV ({order.length.toLocaleString()} rows)
        </button>
      </div>
      <TableScroll
        ref={scroller}
        className="overflow-auto"
        style={{ maxHeight: ROW_HEIGHT * VIEWPORT_ROWS + 28 }}
        onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
      >
        <table aria-label="Log table" className="w-full text-left text-xs font-num">
          <thead className="sticky top-0 bg-elev text-fg-faint">
            <tr>
              <th>
                <button type="button" onClick={() => toggle(TIME_KEY)}>
                  time{arrow(TIME_KEY)}
                </button>
              </th>
              {frame.columns.map((column) => (
                <th key={column.spec}>
                  <button type="button" onClick={() => toggle(column.spec)}>
                    {column.spec}
                    {column.unit ? ` (${column.unit})` : ""}
                    {arrow(column.spec)}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {first > 0 && (
              <tr aria-hidden="true" style={{ height: first * ROW_HEIGHT }}>
                <td colSpan={specs.length + 1} />
              </tr>
            )}
            {visible.map((i) => (
              <tr
                key={i}
                data-row
                style={{ height: ROW_HEIGHT }}
                className="border-t border-border-soft"
              >
                <td>{new Date(frame.timestamps[i]).toLocaleTimeString()}</td>
                {specs.map((spec) => (
                  <td key={spec}>{formatFrameValue(frame.values[spec]?.[i])}</td>
                ))}
              </tr>
            ))}
            {last < order.length && (
              <tr aria-hidden="true" style={{ height: (order.length - last) * ROW_HEIGHT }}>
                <td colSpan={specs.length + 1} />
              </tr>
            )}
          </tbody>
        </table>
      </TableScroll>
    </div>
  );
}

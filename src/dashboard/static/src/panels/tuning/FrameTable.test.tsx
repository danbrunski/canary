import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { frame, longFrame } from "../../test/workbenchFixtures";
import { downloadText } from "../../util/browser";
import { FrameTable } from "./FrameTable";

vi.mock("../../util/browser", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../util/browser")>();
  return { ...actual, downloadText: vi.fn() };
});
const download = vi.mocked(downloadText);

afterEach(() => vi.clearAllMocks());

/** The table's first column of body cells, in display order. */
function column(index: number): string[] {
  const table = screen.getByRole("table", { name: "Log table" });
  return Array.from(table.querySelectorAll("tr[data-row]")).map(
    (row) => row.querySelectorAll("td")[index].textContent ?? "",
  );
}
const header = (name: RegExp) =>
  within(screen.getByRole("table", { name: "Log table" })).getByRole("button", { name });
const rows = () => document.querySelectorAll("tr[data-row]");
const spacers = () =>
  Array.from(document.querySelectorAll('tr[aria-hidden="true"]')).map(
    (row) => (row as HTMLElement).style.height,
  );

describe("FrameTable sorting", () => {
  const withGaps = frame({
    values: { "obd:RPM": [800, null, 1500], "obd:MAF": [null, 60, 20] },
  });

  it("starts in time order, and the time header reverses it", () => {
    render(<FrameTable frame={frame()} />);
    expect(column(1)).toEqual(["800", "3000", "1500"]);
    expect(header(/time/)).toHaveTextContent("time ▲");
    fireEvent.click(header(/time/));
    expect(column(1)).toEqual(["1500", "3000", "800"]);
    expect(header(/time/)).toHaveTextContent("time ▼");
  });

  it("sorts a channel ascending, then descending, then ascending again", () => {
    render(<FrameTable frame={frame()} />);
    fireEvent.click(header(/obd:RPM/));
    expect(column(1)).toEqual(["800", "1500", "3000"]);
    expect(header(/obd:RPM/)).toHaveTextContent("obd:RPM (rpm) ▲");
    expect(header(/time/)).not.toHaveTextContent("▲");
    fireEvent.click(header(/obd:RPM/));
    expect(column(1)).toEqual(["3000", "1500", "800"]);
    expect(header(/obd:RPM/)).toHaveTextContent("obd:RPM (rpm) ▼");
    fireEvent.click(header(/obd:RPM/));
    expect(column(1)).toEqual(["800", "1500", "3000"]);
  });

  it("starts a different column ascending whatever the last direction was", () => {
    render(<FrameTable frame={frame()} />);
    fireEvent.click(header(/obd:RPM/));
    fireEvent.click(header(/obd:RPM/));
    fireEvent.click(header(/obd:MAF/));
    expect(column(2)).toEqual(["4", "20", "60"]);
    expect(header(/obd:MAF/)).toHaveTextContent("▲");
  });

  it("puts a missing value last in both directions", () => {
    render(<FrameTable frame={withGaps} />);
    fireEvent.click(header(/obd:RPM/));
    expect(column(1)).toEqual(["800", "1500", ""]);
    fireEvent.click(header(/obd:RPM/));
    expect(column(1)).toEqual(["1500", "800", ""]);
    fireEvent.click(header(/obd:MAF/));
    expect(column(2)).toEqual(["20", "60", ""]);
    fireEvent.click(header(/obd:MAF/));
    expect(column(2)).toEqual(["60", "20", ""]);
  });

  it("formats values to three places without trailing zeros, whole numbers from 1000", () => {
    render(
      <FrameTable
        frame={frame({
          values: { "obd:RPM": [12.5, 1234.567, 0.12345], "obd:MAF": [3, null, 2.5] },
        })}
      />,
    );
    expect(column(1)).toEqual(["12.5", "1235", "0.123"]);
    expect(column(2)).toEqual(["3", "", "2.5"]);
  });
});

describe("FrameTable virtualization", () => {
  const long = longFrame(500);

  it("renders a window of rows with spacer rows standing in for the rest", () => {
    render(<FrameTable frame={long} />);
    // 18 visible rows plus 20 overscan below at the top of the scroll.
    expect(rows()).toHaveLength(58);
    expect(column(1)[0]).toBe("1000");
    expect(column(1)[57]).toBe("1570");
    expect(spacers()).toEqual([`${(500 - 58) * 24}px`]);
  });

  it("moves the window with the scroll position, 20 rows of overscan each way", () => {
    render(<FrameTable frame={long} />);
    const scroller = document.querySelector("[data-table-scroll]") as HTMLElement;
    fireEvent.scroll(scroller, { target: { scrollTop: 2400 } });
    // Row 100 is the first in view; rendering starts 20 above it.
    expect(rows()).toHaveLength(58);
    expect(column(1)[0]).toBe("1800");
    expect(spacers()).toEqual([`${80 * 24}px`, `${(500 - 138) * 24}px`]);
  });

  it("stops the window at the last row with no spacer below", () => {
    render(<FrameTable frame={long} />);
    const scroller = document.querySelector("[data-table-scroll]") as HTMLElement;
    fireEvent.scroll(scroller, { target: { scrollTop: 499 * 24 } });
    expect(rows()).toHaveLength(21);
    expect(column(1).at(-1)).toBe("5990");
    expect(spacers()).toEqual([`${479 * 24}px`]);
  });

  it("windows the sorted order, not the frame order", () => {
    render(<FrameTable frame={long} />);
    fireEvent.click(header(/obd:RPM/));
    fireEvent.click(header(/obd:RPM/));
    expect(column(1)[0]).toBe("5990");
    const scroller = document.querySelector("[data-table-scroll]") as HTMLElement;
    fireEvent.scroll(scroller, { target: { scrollTop: 2400 } });
    expect(column(1)[0]).toBe("5190");
  });

  it("caps the scroll box at 18 rows plus the header, and keeps it keyboard-focusable", () => {
    render(<FrameTable frame={long} />);
    const scroller = document.querySelector("[data-table-scroll]") as HTMLElement;
    expect(scroller.style.maxHeight).toBe(`${24 * 18 + 28}px`);
    expect(scroller).toHaveAttribute("tabindex", "0");
    for (const row of rows()) expect((row as HTMLElement).style.height).toBe("24px");
  });
});

describe("FrameTable export", () => {
  it("exports every row in the order shown, with units, to a regime-named file", () => {
    render(
      <FrameTable
        frame={frame({ values: { "obd:RPM": [800, null, 1500], "obd:MAF": [4, 60, 20] } })}
      />,
    );
    expect(screen.getByRole("button", { name: "Export CSV (3 rows)" })).toBeInTheDocument();
    fireEvent.click(header(/obd:RPM/));
    fireEvent.click(header(/obd:RPM/));
    fireEvent.click(screen.getByRole("button", { name: /Export CSV/ }));
    expect(download).toHaveBeenCalledTimes(1);
    const [text, name, type] = download.mock.calls[0];
    expect(text).toBe(
      [
        "timestamp,obd:RPM (rpm),obd:MAF (g/s)",
        "2026-09-20T12:00:02+00:00,1500,20",
        "2026-09-20T12:00:00+00:00,800,4",
        "2026-09-20T12:00:01+00:00,,60",
        "",
      ].join("\n"),
    );
    expect(name).toBe("_capture-tuning-2026-09-20T120000-all.csv");
    expect(type).toBe("text/csv");
  });

  it("exports the whole frame, not only the rows in the window", () => {
    render(<FrameTable frame={longFrame(500)} />);
    expect(screen.getByRole("button", { name: "Export CSV (500 rows)" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Export CSV/ }));
    const text = download.mock.calls[0][0];
    expect(text.split("\n")).toHaveLength(502);
  });

  it("names the regime in the file and leaves a column without a unit bare", () => {
    render(
      <FrameTable
        frame={frame({
          columns: [{ spec: "obd:RPM", unit: null }],
          values: { "obd:RPM": [1, 2, 3] },
          regime_report: { regime: "fuel_cut", before: 3, after: 3 },
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Export CSV/ }));
    const [text, name] = download.mock.calls[0];
    expect(text.split("\n")[0]).toBe("timestamp,obd:RPM");
    expect(name).toBe("_capture-tuning-2026-09-20T120000-fuel_cut.csv");
  });
});

describe("FrameTable empty", () => {
  it("says so, with no table and nothing to export", () => {
    render(
      <FrameTable
        frame={frame({
          rows_total: 0,
          rows_returned: 0,
          timestamps: [],
          values: { "obd:RPM": [], "obd:MAF": [] },
        })}
      />,
    );
    expect(screen.getByText("No rows in this view.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Export CSV/ })).not.toBeInTheDocument();
  });
});

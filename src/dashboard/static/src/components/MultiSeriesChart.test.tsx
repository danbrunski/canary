import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MultiSeriesChart } from "./MultiSeriesChart";

const SERIES = [
  { key: "obd:RPM", color: "#fff", values: [800, 1500, 3000] },
  { key: "obd:MAF", color: "#000", values: [4, 20] },
];
const STAMPS = [
  "2026-09-20T12:00:00+00:00",
  "2026-09-20T12:00:01+00:00",
  "2026-09-20T12:00:02+00:00",
];

/** jsdom's Blob has no `.text()`. */
function readBlob(blob: Blob): Promise<string> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.readAsText(blob);
  });
}

describe("MultiSeriesChart", () => {
  let blobs: Blob[];
  let clicked: Array<{ href: string; download: string }>;

  beforeEach(() => {
    blobs = [];
    clicked = [];
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn((blob: Blob) => {
        blobs.push(blob);
        return "blob:chart";
      }),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked.push({ href: this.href, download: this.download });
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("exports the plotted window as CSV with a timestamp column", async () => {
    render(<MultiSeriesChart series={SERIES} timestamps={STAMPS} />);
    fireEvent.click(screen.getByRole("button", { name: "export CSV" }));
    expect(clicked).toEqual([{ href: "blob:chart", download: "chart.csv" }]);
    expect(blobs[0].type).toBe("text/csv");
    expect(await readBlob(blobs[0])).toBe(
      [
        "index,timestamp,obd:RPM,obd:MAF",
        `0,${STAMPS[0]},800,4`,
        `1,${STAMPS[1]},1500,20`,
        `2,${STAMPS[2]},3000,`,
      ].join("\n"),
    );
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:chart");
  });

  it("exports without a timestamp column when none were given", async () => {
    render(<MultiSeriesChart series={SERIES.slice(0, 1)} />);
    fireEvent.click(screen.getByRole("button", { name: "export CSV" }));
    expect(await readBlob(blobs[0])).toBe("index,obd:RPM\n0,800\n1,1500\n2,3000");
  });

  it("shows no export toolbar for an empty chart or when hidden", () => {
    const { rerender } = render(<MultiSeriesChart series={[]} />);
    expect(screen.queryByRole("button", { name: "export CSV" })).not.toBeInTheDocument();
    rerender(<MultiSeriesChart series={SERIES} showExport={false} />);
    expect(screen.queryByRole("button", { name: "export CSV" })).not.toBeInTheDocument();
  });

  it("reads out every series at the hovered sample", () => {
    const { container } = render(<MultiSeriesChart series={SERIES} width={200} />);
    const canvas = container.querySelector("canvas")!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({
      left: 0,
      width: 200,
    } as DOMRect);
    fireEvent.pointerMove(canvas, { clientX: 100, pointerType: "mouse" });
    expect(screen.getByText("1500")).toBeInTheDocument();
    expect(screen.getByText("20")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "clear readout" })).not.toBeInTheDocument();
    fireEvent.pointerLeave(canvas, { pointerType: "mouse" });
    expect(screen.queryByText("1500")).not.toBeInTheDocument();
  });

  // P4.2: a phone has no hover. A touch reads the sample under the finger,
  // dragging moves it, and the readout outlives the finger lifting.
  it("reads values by touch and keeps the readout until it is cleared", () => {
    const { container } = render(<MultiSeriesChart series={SERIES} width={200} />);
    const canvas = container.querySelector("canvas")!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({
      left: 0,
      width: 200,
    } as DOMRect);
    fireEvent.pointerDown(canvas, { clientX: 0, pointerType: "touch" });
    expect(screen.getByTestId("chart-readout")).toHaveTextContent("800");
    fireEvent.pointerMove(canvas, { clientX: 200, pointerType: "touch" });
    expect(screen.getByTestId("chart-readout")).toHaveTextContent("3000");
    fireEvent.pointerUp(canvas, { clientX: 200, pointerType: "touch" });
    fireEvent.pointerLeave(canvas, { pointerType: "touch" });
    expect(screen.getByTestId("chart-readout")).toHaveTextContent("3000");
    fireEvent.click(screen.getByRole("button", { name: "clear readout" }));
    expect(screen.queryByTestId("chart-readout")).not.toBeInTheDocument();
  });

  it("follows a controlled cursor so stacked charts read the same instant", () => {
    const onCursorChange = vi.fn();
    const { container, rerender } = render(
      <MultiSeriesChart
        series={SERIES}
        width={200}
        cursor={null}
        onCursorChange={onCursorChange}
      />,
    );
    const canvas = container.querySelector("canvas")!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({
      left: 0,
      width: 200,
    } as DOMRect);
    fireEvent.pointerDown(canvas, { clientX: 100, pointerType: "touch" });
    expect(onCursorChange).toHaveBeenLastCalledWith(1);
    expect(screen.queryByTestId("chart-readout")).not.toBeInTheDocument();
    rerender(
      <MultiSeriesChart series={SERIES} width={200} cursor={2} onCursorChange={onCursorChange} />,
    );
    expect(screen.getByTestId("chart-readout")).toHaveTextContent("3000");
  });
});

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
    fireEvent.mouseMove(canvas, { clientX: 100 });
    expect(screen.getByText("1500")).toBeInTheDocument();
    expect(screen.getByText("20")).toBeInTheDocument();
    fireEvent.mouseLeave(canvas);
    expect(screen.queryByText("1500")).not.toBeInTheDocument();
  });
});

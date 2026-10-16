import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { frame } from "../../test/workbenchFixtures";
import { FrameVisual } from "./FrameVisual";

const strips = () => within(screen.getByLabelText("Time-series strips"));
const points = () =>
  Array.from(document.querySelectorAll("polyline")).map((p) => p.getAttribute("points"));
const captions = () =>
  Array.from(document.querySelectorAll("figcaption")).map((c) => c.textContent);
const circles = () => Array.from(document.querySelectorAll('svg[aria-label="Scatter"] circle'));
const cells = () => Array.from(document.querySelectorAll('svg[aria-label="Heat map"] rect'));
const select = (label: string) => screen.getByLabelText<HTMLSelectElement>(label);

describe("FrameVisual strips", () => {
  it("draws one strip per channel on a shared time axis, scaled to its own range", () => {
    render(<FrameVisual frame={frame()} />);
    expect(strips().getAllByRole("figure")).toHaveLength(2);
    // t0, t0 + 1 s, t0 + 2 s across 720 units; y inside a 4-unit margin of 70.
    expect(points()).toEqual(["0.0,66.0 360.0,4.0 720.0,46.3", "0.0,66.0 360.0,4.0 720.0,48.3"]);
    expect(captions().slice(0, 2)).toEqual(["obd:RPM (rpm) · 800–3000", "obd:MAF (g/s) · 4–60"]);
  });

  it("draws across a missing sample rather than breaking the line", () => {
    render(
      <FrameVisual
        frame={frame({ values: { "obd:RPM": [800, null, 1500], "obd:MAF": [4, 60, 20] } })}
      />,
    );
    // The null cell contributes no point; its neighbours are joined.
    expect(points()[0]).toBe("0.0,66.0 720.0,4.0");
    expect(captions()[0]).toBe("obd:RPM (rpm) · 800–1500");
  });

  it("puts a constant channel on the floor of its strip", () => {
    render(
      <FrameVisual frame={frame({ values: { "obd:RPM": [5, 5, 5], "obd:MAF": [4, 60, 20] } })} />,
    );
    expect(points()[0]).toBe("0.0,66.0 360.0,66.0 720.0,66.0");
    expect(captions()[0]).toBe("obd:RPM (rpm) · 5–5");
  });

  it("draws an empty strip, without a range, for a channel with no values", () => {
    render(
      <FrameVisual
        frame={frame({ values: { "obd:RPM": [null, null, null], "obd:MAF": [4, 60, 20] } })}
      />,
    );
    expect(points()[0]).toBe("");
    expect(captions()[0]).toBe("obd:RPM (rpm)");
  });

  it("marks tags and flashes inside the span on every strip, and none outside it", () => {
    const { container } = render(
      <FrameVisual
        frame={frame({
          tags: [
            { t: "2026-09-20T12:00:00+00:00", label: "at start" },
            { t: "2026-09-20T12:00:01+00:00", label: "pull_start" },
            { t: "2026-09-20T12:00:02+00:00", label: "at end" },
            { t: "2026-09-20T11:59:59+00:00", label: "before" },
            { t: "2026-09-20T12:00:03+00:00", label: "after" },
          ],
          revision_boundaries: [
            { revision: "B", flashed_at: "2026-09-20T12:00:01+00:00" },
            { revision: "C", flashed_at: "2026-09-21T12:00:00+00:00" },
          ],
        })}
      />,
    );
    const tags = Array.from(container.querySelectorAll('[data-marker="tag"]'));
    const flashes = Array.from(container.querySelectorAll('[data-marker="flash"]'));
    // Both span ends are inside it; two strips each carry every marker.
    expect(tags.map((m) => m.getAttribute("x1"))).toEqual(["0", "360", "720", "0", "360", "720"]);
    expect(flashes.map((m) => m.getAttribute("x1"))).toEqual(["360", "360"]);
    expect(flashes[0].textContent).toBe("flash B");
    expect(flashes[0]).toHaveClass("stroke-bad");
    expect(flashes[0]).not.toHaveAttribute("stroke-dasharray");
    expect(tags[0]).toHaveClass("stroke-fg-dim");
    expect(tags[0]).toHaveAttribute("stroke-dasharray", "3 3");
  });

  it("says there are no rows, and draws nothing, for an empty frame", () => {
    render(
      <FrameVisual
        frame={frame({
          rows_total: 0,
          rows_returned: 0,
          timestamps: [],
          values: { "obd:RPM": [], "obd:MAF": [] },
        })}
      />,
    );
    expect(screen.getByText("No rows in this view.")).toBeInTheDocument();
    expect(document.querySelector("svg")).toBeNull();
  });
});

describe("FrameVisual scatter", () => {
  it("plots the first channel against the second, one point per paired sample", () => {
    render(<FrameVisual frame={frame()} />);
    expect(select("X axis").value).toBe("obd:RPM");
    expect(select("Y axis").value).toBe("obd:MAF");
    const at = circles().map((c) => [Number(c.getAttribute("cx")), Number(c.getAttribute("cy"))]);
    // 360 x 240 with a 5 unit margin; y grows upward.
    expect(at[0]).toEqual([5, 235]);
    expect(at[1]).toEqual([355, 5]);
    expect(at[2][0]).toBeCloseTo(116.3636, 3);
    expect(at[2][1]).toBeCloseTo(169.2857, 3);
    expect(captions().at(-1)).toBe("obd:MAF against obd:RPM · x 800–3000 · y 4–60");
  });

  it("skips a sample missing either axis, but keeps one missing only its colour", () => {
    render(
      <FrameVisual
        frame={frame({
          columns: [
            { spec: "obd:RPM", unit: "rpm" },
            { spec: "obd:MAF", unit: "g/s" },
            { spec: "obd:TPS", unit: "%" },
          ],
          values: {
            "obd:RPM": [800, null, 1500, 2000],
            "obd:MAF": [4, 60, null, 30],
            "obd:TPS": [1, 2, 3, null],
          },
          timestamps: [
            "2026-09-20T12:00:00+00:00",
            "2026-09-20T12:00:01+00:00",
            "2026-09-20T12:00:02+00:00",
            "2026-09-20T12:00:03+00:00",
          ],
        })}
      />,
    );
    // Colour defaults to the third channel here; the last sample has no value for it.
    expect(select("Colour channel").value).toBe("obd:TPS");
    expect(circles()).toHaveLength(2);
    expect(circles()[1]).toHaveStyle({ fill: "var(--color-accent-2)" });
  });

  it("colours by the chosen channel, low to high on the sequential ramp", () => {
    render(<FrameVisual frame={frame()} />);
    expect(circles()[0]).toHaveStyle({ fill: "var(--color-accent-2)" });
    fireEvent.change(select("Colour channel"), { target: { value: "obd:MAF" } });
    expect(circles()[0]).toHaveStyle({ fill: "rgb(80, 190, 110)" });
    expect(circles()[1]).toHaveStyle({ fill: "rgb(230, 80, 65)" });
    expect(captions().at(-1)).toBe(
      "obd:MAF against obd:RPM, coloured by obd:MAF · x 800–3000 · y 4–60",
    );
  });

  it("uses the one channel for both axes when there is only one", () => {
    render(
      <FrameVisual
        frame={frame({
          columns: [{ spec: "obd:RPM", unit: "rpm" }],
          values: { "obd:RPM": [800, 3000, 1500] },
        })}
      />,
    );
    expect(select("X axis").value).toBe("obd:RPM");
    expect(select("Y axis").value).toBe("obd:RPM");
    expect(select("Colour channel").value).toBe("");
    expect(circles()).toHaveLength(3);
  });

  it("falls back to the default axis when the next frame no longer carries a chosen channel", () => {
    const three = frame({
      columns: [
        { spec: "obd:RPM", unit: "rpm" },
        { spec: "obd:MAF", unit: "g/s" },
        { spec: "obd:TPS", unit: "%" },
      ],
      values: { "obd:RPM": [800, 3000, 1500], "obd:MAF": [4, 60, 20], "obd:TPS": [1, 2, 3] },
    });
    const { rerender } = render(<FrameVisual frame={three} />);
    fireEvent.change(select("X axis"), { target: { value: "obd:TPS" } });
    fireEvent.change(select("Colour channel"), { target: { value: "obd:TPS" } });
    expect(select("X axis").value).toBe("obd:TPS");
    rerender(<FrameVisual frame={frame()} />);
    expect(select("X axis").value).toBe("obd:RPM");
    expect(select("Colour channel").value).toBe("");
    // The choice is masked, not forgotten: a frame that carries it again shows it.
    rerender(<FrameVisual frame={three} />);
    expect(select("X axis").value).toBe("obd:TPS");
  });
});

describe("FrameVisual heat map", () => {
  const toHeat = () => fireEvent.click(screen.getByRole("button", { name: "Heat map" }));

  it("counts samples per cell of a 12 x 12 grid, the maximum landing in the last cell", () => {
    render(<FrameVisual frame={frame()} />);
    toHeat();
    expect(select("Colour channel").previousSibling?.textContent).toBe("Cell value (mean)");
    expect(
      within(select("Colour channel")).getByRole("option", { name: "sample count" }),
    ).toBeTruthy();
    const geometry = cells().map((c) =>
      ["x", "y", "width", "height"].map((a) => Number(c.getAttribute(a))),
    );
    // x bin 0 / 11 / 3 of 30-wide cells, y bin 0 / 11 / 3 of 20-high cells, y up.
    expect(geometry).toEqual([
      [0, 220, 30, 20],
      [330, 0, 30, 20],
      [90, 160, 30, 20],
    ]);
    expect(cells().map((c) => c.textContent)).toEqual(["1 (n 1)", "1 (n 1)", "1 (n 1)"]);
    expect(captions().at(-1)).toBe(
      "obd:MAF against obd:RPM, cell sample count · x 800–3000 · y 4–60",
    );
  });

  it("merges samples that share a cell, and colours density by count", () => {
    render(
      <FrameVisual
        frame={frame({ values: { "obd:RPM": [800, 810, 3000], "obd:MAF": [4, 5, 60] } })}
      />,
    );
    toHeat();
    expect(cells().map((c) => c.textContent)).toEqual(["2 (n 2)", "1 (n 1)"]);
    expect(cells()[0]).toHaveAttribute("fill", "rgb(230, 80, 65)");
    expect(cells()[1]).toHaveAttribute("fill", "rgb(80, 190, 110)");
  });

  it("averages the colour channel per cell, and drops a sample missing it", () => {
    render(
      <FrameVisual
        frame={frame({
          columns: [
            { spec: "obd:RPM", unit: "rpm" },
            { spec: "obd:MAF", unit: "g/s" },
            { spec: "obd:TPS", unit: "%" },
          ],
          values: {
            "obd:RPM": [800, 810, 3000],
            "obd:MAF": [4, 5, 60],
            "obd:TPS": [10, 20, null],
          },
        })}
      />,
    );
    toHeat();
    expect(select("Colour channel").value).toBe("obd:TPS");
    // Two samples share the first cell (mean 15); the third has no TPS and is not counted.
    expect(cells().map((c) => c.textContent)).toEqual(["15 (n 2)"]);
    expect(captions().at(-1)).toContain(", cell mean of obd:TPS");
  });

  it("puts every sample in the first cell along an axis that does not vary", () => {
    render(
      <FrameVisual frame={frame({ values: { "obd:RPM": [5, 5, 5], "obd:MAF": [1, 2, 3] } })} />,
    );
    toHeat();
    const xs = cells().map((c) => c.getAttribute("x"));
    expect(xs).toEqual(["0", "0", "0"]);
    expect(cells().map((c) => c.getAttribute("y"))).toEqual(["220", "100", "0"]);
  });

  it("toggles back to the scatter with the same axes", () => {
    render(<FrameVisual frame={frame()} />);
    toHeat();
    fireEvent.click(screen.getByRole("button", { name: "Scatter" }));
    expect(screen.getByLabelText("Scatter")).toBeInTheDocument();
    expect(circles()).toHaveLength(3);
    expect(select("Colour channel").previousSibling?.textContent).toBe("Colour");
  });

  it("draws no cells when an axis has no values at all", () => {
    render(
      <FrameVisual
        frame={frame({ values: { "obd:RPM": [null, null, null], "obd:MAF": [4, 60, 20] } })}
      />,
    );
    toHeat();
    expect(screen.getByLabelText("Heat map")).toBeInTheDocument();
    expect(cells()).toHaveLength(0);
  });
});

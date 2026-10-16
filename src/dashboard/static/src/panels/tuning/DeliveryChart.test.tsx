import { cleanup, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FLOOR, delivery } from "../../test/workbenchFixtures";
import { DeliveryChart } from "./DeliveryChart";

const rect = (band: string) =>
  document.querySelector(`[data-band="${band}"] rect`) as SVGRectElement;
const num = (el: Element, attr: string) => Number(el.getAttribute(attr));

describe("DeliveryChart", () => {
  it("scales to the larger of the errors and the floor, with 3 % as the least span", () => {
    const { container } = render(<DeliveryChart result={delivery()} />);
    const svg = container.querySelector("svg") as SVGSVGElement;
    // Two bands fit the 320-wide minimum; the chart is 200 high plus a 20 label strip.
    expect(svg).toHaveAttribute("width", "320");
    expect(svg).toHaveAttribute("height", "220");
    // limit = max(3, 2.0, 2.38) * 1.15 = 3.45; y(v) = 100 - v / limit * 88.
    const first = rect("(2500.0, 3000.0]");
    expect(num(first, "x")).toBeCloseTo(32, 6);
    expect(num(first, "width")).toBeCloseTo(96, 6);
    expect(num(first, "height")).toBeCloseTo((2.0 / 3.45) * 88, 1);
    expect(num(first, "y")).toBeCloseTo(100 - (2.0 / 3.45) * 88, 1);
    // The floor: the sd band and the wider per-cell range, centred on zero.
    const sd = container.querySelector('[data-floor="sd"]') as SVGRectElement;
    expect(num(sd, "height")).toBeCloseTo((2 * 1.49 * 88) / 3.45, 1);
    const range = container.querySelector('[data-floor="range"]') as SVGRectElement;
    expect(num(range, "y")).toBeCloseTo(100 - (1.38 / 3.45) * 88, 1);
    expect(num(range, "height")).toBeCloseTo(((1.38 + 2.38) / 3.45) * 88, 1);
  });

  it("widens the span to a floor range or an error beyond 3 %", () => {
    const base = delivery();
    render(<DeliveryChart result={delivery({ noise_floor: { ...FLOOR, min: -5, max: 1 } })} />);
    // limit = 5 * 1.15 = 5.75.
    expect(num(rect("(2500.0, 3000.0]"), "height")).toBeCloseTo((2.0 / 5.75) * 88, 1);
    cleanup();
    render(<DeliveryChart result={delivery({ bands: [{ ...base.bands[0], error_pct: -4 }] })} />);
    // limit = 4 * 1.15 = 4.6; a negative error hangs below the zero line.
    expect(num(rect("(2500.0, 3000.0]"), "height")).toBeCloseTo((4 / 4.6) * 88, 1);
    expect(num(rect("(2500.0, 3000.0]"), "y")).toBeCloseTo(100, 6);
  });

  it("uses a floor's sd for its range when no range was recorded", () => {
    const { container } = render(
      <DeliveryChart
        result={delivery({ noise_floor: { ...FLOOR, min: null, max: null, sd: 1 } })}
      />,
    );
    const range = container.querySelector('[data-floor="range"]') as SVGRectElement;
    const sd = container.querySelector('[data-floor="sd"]') as SVGRectElement;
    expect(range.getAttribute("y")).toBe(sd.getAttribute("y"));
    expect(range.getAttribute("height")).toBe(sd.getAttribute("height"));
  });

  it("greys a bar inside the floor, and tints by sign outside it", () => {
    const base = delivery();
    const bands = [
      { ...base.bands[0], rpm_band: "in", error_pct: 1.0 },
      { ...base.bands[0], rpm_band: "high", error_pct: 3.0 },
      { ...base.bands[0], rpm_band: "low", error_pct: -3.0 },
    ];
    render(<DeliveryChart result={delivery({ bands })} />);
    expect(rect("in")).toHaveClass("fill-fg-dim");
    expect(rect("high")).toHaveClass("fill-accent");
    expect(rect("low")).toHaveClass("fill-accent-2");
  });

  it("draws no floor, and never greys a bar, when no floor was measured", () => {
    const base = delivery();
    const { container } = render(
      <DeliveryChart
        result={delivery({
          noise_floor: { available: false, channel: "delivery_error_pct" },
          bands: [{ ...base.bands[0], rpm_band: "in", error_pct: 0.5 }],
        })}
      />,
    );
    expect(container.querySelector("[data-floor]")).toBeNull();
    expect(rect("in")).toHaveClass("fill-accent");
    // 0.5 % on a 3.45 % span.
    expect(num(rect("in"), "height")).toBeCloseTo((0.5 / 3.45) * 88, 1);
  });

  it("labels every bar with its error and n, and treats a missing error as zero", () => {
    const base = delivery();
    const bands = Array.from({ length: 6 }, (_, i) => ({
      ...base.bands[0],
      rpm_band: `b${i}`,
      n: i + 1,
      error_pct: i === 2 ? null : 1,
    }));
    const { container } = render(<DeliveryChart result={delivery({ bands })} />);
    // Six bands widen the chart 70 each.
    expect(container.querySelector("svg")).toHaveAttribute("width", "420");
    expect(num(rect("b2"), "height")).toBe(0);
    const labels = Array.from(container.querySelectorAll("text")).map((t) => t.textContent);
    expect(labels).toContain("0.00% · n 3");
    expect(labels).toContain("1.00% · n 1");
    expect(labels).toContain("b2");
  });

  it("scrolls inside its own box, so a wide chart cannot widen the page", () => {
    const base = delivery();
    const bands = Array.from({ length: 6 }, (_, i) => ({
      ...base.bands[0],
      band: `band ${i}`,
    }));
    const { container } = render(<DeliveryChart result={delivery({ bands })} />);
    const svg = container.querySelector("svg") as SVGSVGElement;
    // Six bands are 6 * 70 = 420 px wide, wider than a 412 px phone.
    expect(svg).toHaveAttribute("width", "420");
    expect(svg.parentElement).toHaveClass("overflow-x-auto");
  });
});

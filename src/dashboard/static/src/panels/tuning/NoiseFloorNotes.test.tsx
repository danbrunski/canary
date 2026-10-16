import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FLOOR } from "../../test/workbenchFixtures";
import type { NoiseFloor, Significance } from "../../types/workbench";
import { FloorNote, SignificanceNote } from "./NoiseFloorNotes";

const refused = (
  overrides: Partial<Extract<Significance, { available: false }>> = {},
): Significance => ({
  available: false,
  channel: "timing_advance_deg",
  kind: "conditions_mismatch",
  reason: "the floor was measured elsewhere",
  floor_conditions: "WOT: throttle>=85",
  requested_conditions: "all conditions",
  ...overrides,
});
const accepted: Significance = {
  available: true,
  channel: "timing_advance_deg",
  sd: 1.49,
  conditions: "WOT",
  sigma_threshold: 2,
};

describe("FloorNote", () => {
  it("says nothing when the result carries no floor at all", () => {
    const { container } = render(<FloorNote floor={null} />);
    expect(container).toBeEmptyDOMElement();
    const again = render(<FloorNote floor={undefined} />);
    expect(again.container).toBeEmptyDOMElement();
  });

  it("states a floor that was never measured, apart from one that was", () => {
    render(<FloorNote floor={{ available: false, channel: "x", reason: "none" }} />);
    expect(screen.getByText("Noise floor: not measured for this vehicle.")).toBeInTheDocument();
    expect(screen.queryByText(/null run/)).not.toBeInTheDocument();
  });

  it("states the sd, range, cells and conditions, with percent spelled as %", () => {
    render(<FloorNote floor={FLOOR} />);
    expect(screen.getByText(/Noise floor \(null run\)/)).toHaveTextContent(
      "Noise floor (null run): sd 1.49 %, per-cell range -2.38 … 1.38 over 5 cells. " +
        "Measured under: WOT: throttle>=85, 3000-5000 rpm. " +
        "A result inside the band is not distinguishable from changing nothing.",
    );
  });

  it("keeps another unit as it is, and says a range was not recorded rather than inventing one", () => {
    const floor: NoiseFloor = { ...FLOOR, unit: "deg", sd: 0.5, min: null, max: 1 };
    render(<FloorNote floor={floor} />);
    expect(screen.getByText(/Noise floor \(null run\)/)).toHaveTextContent(
      "sd 0.50 deg, per-cell range not recorded over 5 cells.",
    );
  });
});

describe("SignificanceNote", () => {
  it("names a refused floor with both regimes, and draws no floor", () => {
    render(<SignificanceNote floor={FLOOR} significance={refused()} />);
    const note = screen.getByRole("note", { name: "Noise floor refused" });
    expect(note).toHaveTextContent(
      'Noise floor refused for timing_advance_deg: measured under "WOT: throttle>=85", ' +
        'but this comparison is "all conditions". No cell here has a significance verdict.',
    );
    expect(screen.queryByText(/null run/)).not.toBeInTheDocument();
  });

  it("gives the reason instead when there was no floor to quote conditions from", () => {
    render(
      <SignificanceNote
        floor={null}
        significance={refused({
          kind: "no_floor",
          floor_conditions: null,
          reason: "no floor measured",
        })}
      />,
    );
    expect(screen.getByRole("note", { name: "Noise floor refused" })).toHaveTextContent(
      "Noise floor refused for timing_advance_deg: no floor measured No cell here",
    );
  });

  it("falls through to the floor itself when it was accepted, or when none was asked for", () => {
    const first = render(<SignificanceNote floor={FLOOR} significance={accepted} />);
    expect(screen.getByText(/Noise floor \(null run\)/)).toBeInTheDocument();
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    first.unmount();
    const second = render(<SignificanceNote floor={FLOOR} significance={null} />);
    expect(screen.getByText(/Noise floor \(null run\)/)).toBeInTheDocument();
    second.unmount();
    render(<SignificanceNote floor={null} significance={undefined} />);
    expect(screen.queryByText(/Noise floor/)).not.toBeInTheDocument();
  });
});

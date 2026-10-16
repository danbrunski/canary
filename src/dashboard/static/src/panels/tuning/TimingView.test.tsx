import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlJson } from "../../api/core";
import { FLOOR, deferred, pullComparison, timingResponse } from "../../test/workbenchFixtures";
import type { PullComparisonResponse, SessionChoice, TimingResponse } from "../../types/workbench";
import { TimingView } from "./TimingView";

vi.mock("../../api/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/core")>();
  return { ...actual, controlJson: vi.fn() };
});
const control = vi.mocked(controlJson);

afterEach(() => vi.clearAllMocks());

const RUNNING: SessionChoice = { sessions: [], revision: "ZS44A-20260920" };
const HISTORY = { history: [{ revision: "A" }, { revision: "B" }, { revision: "C" }] };
const calls = (prefix: string) =>
  control.mock.calls.filter(([path]) => String(path).startsWith(prefix));
const mode = (name: string) => fireEvent.click(screen.getByRole("button", { name }));
const revisions = (a: string, b: string) => {
  fireEvent.change(screen.getByLabelText("Revision A"), { target: { value: a } });
  fireEvent.change(screen.getByLabelText("Revision B"), { target: { value: b } });
};

function answer(handlers: Record<string, unknown>) {
  control.mockImplementation((path: string) => {
    for (const [prefix, value] of Object.entries(handlers)) {
      if (path.startsWith(prefix)) {
        return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
      }
    }
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}
/** Open Compare with the revision list loaded. */
async function openCompare(comparison: Partial<PullComparisonResponse> = {}) {
  answer({
    "/control/calibration": HISTORY,
    "/analysis/pull-comparison": pullComparison(comparison),
  });
  const view = render(<TimingView token="t" choice={RUNNING} />);
  mode("Compare revisions");
  await screen.findAllByRole("option", { name: "C" });
  return view;
}

describe("TimingView cells", () => {
  it("runs only with a token and a choice, and draws the cells with their conditions", async () => {
    answer({ "/analysis/timing": timingResponse() });
    const { rerender } = render(<TimingView token="t" choice={{ sessions: [], revision: null }} />);
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    rerender(<TimingView token="" choice={RUNNING} />);
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    rerender(<TimingView token="t" choice={RUNNING} />);
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByRole("table", { name: "Timing cells" })).toBeInTheDocument();
    expect(control).toHaveBeenCalledWith(
      "/analysis/timing?revision=ZS44A-20260920",
      "t",
      expect.anything(),
    );
    expect(screen.getByLabelText("Conditions")).toBeInTheDocument();
  });

  it("gives Cells its own empty-result words", async () => {
    answer({
      "/analysis/timing": timingResponse({
        available: false,
        unavailable_kind: "gates_removed_everything",
        gates: { started: 8, filtered: { fuel_cut: 8 }, remaining: 0 },
        cells: [],
      }),
    });
    render(<TimingView token="t" choice={RUNNING} />);
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    const empty = await screen.findAllByText("0 of 8 samples passed: fuel cut removed 8");
    expect(empty[0]).toHaveAttribute("data-empty", "gates");
    expect(screen.queryByRole("table", { name: "Timing cells" })).not.toBeInTheDocument();
  });

  it("does not read the calibration history until Compare is open", async () => {
    answer({ "/analysis/timing": timingResponse() });
    render(<TimingView token="t" choice={RUNNING} />);
    await new Promise((r) => setTimeout(r, 20));
    expect(calls("/control/calibration")).toHaveLength(0);
  });
});

describe("TimingView compare", () => {
  it("will not compare a revision with itself or with nothing", async () => {
    await openCompare();
    const compare = screen.getByRole("button", { name: "Compare" });
    expect(compare).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Revision A"), { target: { value: "A" } });
    expect(compare).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Revision B"), { target: { value: "A" } });
    expect(compare).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Revision B"), { target: { value: "C" } });
    expect(compare).toBeEnabled();
  });

  it("sends the optional noise-floor channel trimmed, or leaves it out", async () => {
    await openCompare();
    revisions("A", "B");
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    await screen.findByRole("table", { name: "Timing comparison" });
    expect(String(calls("/analysis/pull-comparison")[0][0])).toBe(
      "/analysis/pull-comparison?revision_a=A&revision_b=B",
    );
    fireEvent.change(screen.getByLabelText(/Noise-floor channel/), {
      target: { value: " obd:TIMING_ADVANCE " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    await waitFor(() => expect(calls("/analysis/pull-comparison")).toHaveLength(2));
    expect(String(calls("/analysis/pull-comparison")[1][0])).toContain(
      "floor_channel=obd%3ATIMING_ADVANCE",
    );
  });

  it("outlines a dropped cell and sizes each by the thinner of its two sides", async () => {
    await openCompare();
    revisions("A", "B");
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    const grid = await screen.findByRole("table", { name: "Timing comparison" });
    // The outlined cell has 9 samples on A and 7 on B; the other has 9 on each.
    expect(grid.querySelector('[data-flagged="true"]')?.getAttribute("title")).toMatch(/n=7\b/);
    expect(
      grid.querySelector('td:not([data-flagged="true"])[title]')?.getAttribute("title"),
    ).toMatch(/n=9\b/);
    expect(screen.getByText(/1 cell\(s\) outlined/)).toBeInTheDocument();
  });

  it("names why a comparison is empty, and still states the floor that was asked for", async () => {
    await openCompare({
      available: false,
      unavailable_kind: "gates_removed_everything",
      cells: [],
      n_cells: 0,
      gates: { started: 4, filtered: { fuel_cut: 4 }, remaining: 0 },
      noise_floor: { available: false, channel: "timing_advance_deg" },
      significance: null,
    });
    revisions("A", "B");
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    const empty = await screen.findAllByText("0 of 4 samples passed: fuel cut removed 4");
    expect(empty[0]).toHaveAttribute("data-empty", "gates");
    expect(screen.getByText("Noise floor: not measured for this vehicle.")).toBeInTheDocument();
  });

  it.each([
    ["no floor was asked for", { noise_floor: null, significance: null }, null],
    [
      "a floor was never measured",
      { noise_floor: { available: false, channel: "x" }, significance: null },
      /Noise floor: not measured for this vehicle\./,
    ],
    [
      "a floor was measured and accepted",
      {
        noise_floor: { ...FLOOR, channel: "timing_advance_deg", unit: "deg", sd: 0.48 },
        significance: {
          available: true as const,
          channel: "timing_advance_deg",
          sd: 0.48,
          conditions: "WOT",
          sigma_threshold: 2,
        },
      },
      /Noise floor \(null run\): sd 0.48 deg/,
    ],
    [
      "a floor was refused with none measured",
      {
        noise_floor: null,
        significance: {
          available: false as const,
          channel: "timing_advance_deg",
          kind: "no_floor" as const,
          reason: "no floor measured for timing_advance_deg",
          floor_conditions: null,
          requested_conditions: "all conditions",
        },
      },
      /Noise floor refused for timing_advance_deg: no floor measured for timing_advance_deg/,
    ],
  ])("states the floor verdict when %s", async (_name, patch, expected) => {
    await openCompare(patch);
    revisions("A", "B");
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    await screen.findByRole("table", { name: "Timing comparison" });
    const notes = Array.from(document.querySelectorAll("p"))
      .map((p) => p.textContent ?? "")
      .filter((text) => text.startsWith("Noise floor"))
      .join(" | ");
    if (expected === null) expect(notes).toBe("");
    else expect(notes).toMatch(expected);
    // A refused floor is never also drawn as a band.
    if (patch.significance && !patch.significance.available) {
      expect(notes).not.toMatch(/null run/);
    }
  });
});

describe("TimingView independent runs", () => {
  it("keeps a Cells run and a Compare run independent, in flight and after", async () => {
    const cellsReply = deferred<TimingResponse>();
    const compareReply = deferred<PullComparisonResponse>();
    control.mockImplementation((path: string) => {
      if (path.startsWith("/control/calibration")) return Promise.resolve(HISTORY);
      if (path.startsWith("/analysis/timing")) return cellsReply.promise;
      return compareReply.promise;
    });
    render(<TimingView token="t" choice={RUNNING} />);
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByRole("button", { name: "Running…" })).toBeDisabled();

    mode("Compare revisions");
    await screen.findAllByRole("option", { name: "C" });
    revisions("A", "B");
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(await screen.findByRole("button", { name: "Running…" })).toBeDisabled();
    // Starting Compare did not abort the Cells run.
    const cellsSignal = (calls("/analysis/timing")[0][2] as { signal: AbortSignal }).signal;
    expect(cellsSignal.aborted).toBe(false);

    compareReply.resolve(pullComparison());
    await screen.findByRole("table", { name: "Timing comparison" });
    expect(screen.getByRole("button", { name: "Compare" })).toBeEnabled();

    mode("Cells");
    expect(screen.getByRole("button", { name: "Running…" })).toBeDisabled();
    cellsReply.resolve(timingResponse());
    await screen.findByRole("table", { name: "Timing cells" });
    mode("Compare revisions");
    expect(screen.getByRole("table", { name: "Timing comparison" })).toBeInTheDocument();
    // The revision list is read again on re-entering Compare; the choice is kept.
    await screen.findAllByRole("option", { name: "C" });
    expect(screen.getByLabelText("Revision A")).toHaveValue("A");
    expect(screen.getByLabelText("Revision B")).toHaveValue("B");
  });

  it("keeps each mode's refusal under that mode", async () => {
    answer({
      "/control/calibration": HISTORY,
      "/analysis/timing": new HttpError(
        409,
        JSON.stringify({ detail: { error: "analysis_busy", message: "cells busy" } }),
      ),
      "/analysis/pull-comparison": new HttpError(
        409,
        JSON.stringify({
          detail: { error: "analysis_unavailable", message: "compare unavailable" },
        }),
      ),
    });
    render(<TimingView token="t" choice={RUNNING} />);
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByText("cells busy");
    mode("Compare revisions");
    await screen.findAllByRole("option", { name: "C" });
    expect(screen.queryByText("cells busy")).not.toBeInTheDocument();
    revisions("A", "B");
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    await screen.findByText("compare unavailable");
    mode("Cells");
    expect(screen.getByText("cells busy")).toBeInTheDocument();
    expect(screen.queryByText("compare unavailable")).not.toBeInTheDocument();
  });
});

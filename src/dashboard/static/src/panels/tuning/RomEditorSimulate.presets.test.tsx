import { act, cleanup, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { HttpError } from "../../api/core";
import {
  STATED,
  route,
  runButton,
  sentRequest,
  setUpSimulateTests,
  state,
  withPendingEdit,
  TIMINGS_KEY,
  field,
  edgeCount,
  presetButton,
  presetState,
  costBlock,
} from "../../test/romSimulateKit";
import {
  click,
  renderRomEditor,
  routeControl,
  type RouteRequest,
  type,
  bodyOf,
} from "../../test/controlRoutes";
import { ENTRY_TUNED, SHA_TUNED } from "../../test/romLibraryFixtures";
import { showRomView } from "../../test/romEditorViews";
import { documentedSimFields } from "../../test/romSimDoc";
import { buildSimResult, tablesWithAxes } from "../../test/romSimFixtures";
import type { RomSimRequest, RomSimResult } from "../../types/romEditor";

vi.mock("../../api/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/core")>()),
  controlJson: vi.fn(),
  controlBlob: vi.fn(),
}));
vi.mock("../../util/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../util/browser")>()),
  downloadText: vi.fn(),
  downloadBlob: vi.fn(),
}));

setUpSimulateTests();

// -- W09.9: coarse and fine, the cost estimate, Cancel -------------------------------------

/** A simulate answer sized from what the request asked for, as the daemon's would be. */
function sizedAnswer({ init }: RouteRequest): RomSimResult {
  const request = JSON.parse(bodyOf(init)) as RomSimRequest;
  const result = buildSimResult();
  return {
    ...result,
    cells: { ...result.cells, draws: request.draws },
    design: {
      ...result.design,
      points: (request.rpm_edges.length - 1) * (request.load_edges.length - 1),
    },
  };
}

describe("the Coarse and Fine presets", () => {
  it("Coarse fills exactly the edges and the draws, says so, and touches nothing else", async () => {
    route();
    const form = await withPendingEdit();
    state(form, ["RPM edges (rpm)", "Load edges (% fill)", "Draws"]);
    const before = Object.fromEntries(
      Object.keys(STATED)
        .filter((label) => !["RPM edges (rpm)", "Load edges (% fill)", "Draws"].includes(label))
        .map((label) => [label, field(form, label).value]),
    );
    expect(field(form, "Draws").value).toBe("");
    expect(presetState(form)).toHaveTextContent("No preset pressed");
    await click(presetButton(form, "Coarse (quick check)"));
    // Every other breakpoint of 16: 8 cells on each axis, a quarter of 256 points.
    expect(edgeCount(form, "RPM edges (rpm)")).toBe(9);
    expect(edgeCount(form, "Load edges (% fill)")).toBe(9);
    // Every other breakpoint (6.25, 18.75 ... 93.75 %) is 12.5 % apart: edges 0 to 100.
    expect(field(form, "Load edges (% fill)").value).toBe(
      "0, 12.5, 25, 37.5, 50, 62.5, 75, 87.5, 100",
    );
    expect(field(form, "Draws").value).toBe("200");
    expect(within(form).getByLabelText("Grid size")).toHaveTextContent("8 × 8 = 64 of at most");
    expect(presetState(form)).toHaveTextContent(
      "Coarse (quick check) filled RPM edges (9 edges, 8 cells), load edges (9 edges, 8 cells) and draws (200): 64 points × 200 draws. Nothing else was touched.",
    );
    expect(presetButton(form, "Coarse (quick check)")).toHaveAttribute("aria-pressed", "true");
    expect(presetButton(form, "Fine")).toHaveAttribute("aria-pressed", "false");
    // Nothing else moved: the stated environment, the seed and the choices.
    for (const [label, value] of Object.entries(before)) {
      expect(field(form, label).value, label).toBe(value);
    }
    expect(within(form).getByRole("radio", { name: /Fuel Comp axes/ })).toBeChecked();
    // The description is on the page before the click, and names no_points.
    expect(form).toHaveTextContent(
      /Table cells with no visited point show no_points, which is NOT unchanged/,
    );
    expect(runButton(form)).toBeEnabled();
  });

  it("Fine fills the suggested edges (load cut to 100 %) and 1,000 draws", async () => {
    route();
    const form = await withPendingEdit();
    await click(presetButton(form, "Fine"));
    expect(edgeCount(form, "RPM edges (rpm)")).toBe(17);
    expect(edgeCount(form, "Load edges (% fill)")).toBe(17);
    expect(field(form, "Load edges (% fill)").value).toMatch(/96\.875, 100$/);
    expect(field(form, "Draws").value).toBe("1000");
    expect(within(form).getByLabelText("Grid size")).toHaveTextContent("16 × 16 = 256 of at most");
    expect(presetState(form)).toHaveTextContent("Fine filled RPM edges (17 edges, 16 cells)");
    expect(presetState(form)).toHaveTextContent("256 points × 1,000 draws");
    // The seed, the environment and the choices are still the operator's to state.
    expect(field(form, "Seed").value).toBe("");
    expect(field(form, "Baro (kPa)").value).toBe("");
  });

  it("flips to Custom when a filled field is edited, and back when it is typed back", async () => {
    route();
    const form = await withPendingEdit();
    await click(presetButton(form, "Coarse (quick check)"));
    expect(presetState(form)).not.toHaveTextContent("Custom");
    type(field(form, "Draws"), "300");
    expect(presetState(form)).toHaveTextContent(
      "Custom: you edited a field Coarse (quick check) filled",
    );
    expect(presetButton(form, "Coarse (quick check)")).toHaveAttribute("aria-pressed", "false");
    expect(presetButton(form, "Fine")).toHaveAttribute("aria-pressed", "false");
    type(field(form, "Draws"), "200");
    expect(presetButton(form, "Coarse (quick check)")).toHaveAttribute("aria-pressed", "true");
    // An edge edit does it too, and so does Reset to the base's breakpoints.
    type(field(form, "RPM edges (rpm)"), "200, 600, 1000");
    expect(presetState(form)).toHaveTextContent("Custom");
    await click(presetButton(form, "Coarse (quick check)"));
    expect(presetState(form)).not.toHaveTextContent("Custom");
    await click(within(form).getByRole("button", { name: "Reset to the base's breakpoints" }));
    expect(presetState(form)).toHaveTextContent("Custom");
  });

  it("is a button press, not a default: never remembered, and the remembered mark on draws goes", async () => {
    route();
    let form = await withPendingEdit();
    state(form);
    await click(presetButton(form, "Coarse (quick check)"));
    await click(runButton(form));
    await screen.findByRole("region", { name: "Simulation result" });
    const everything = Object.keys(window.localStorage).map(
      (key) => `${key}=${window.localStorage.getItem(key)}`,
    );
    expect(everything.join("\n")).not.toMatch(/coarse|fine|preset/i);

    cleanup();
    vi.clearAllMocks();
    form = await withPendingEdit("again");
    // A fresh visit: no preset pressed, the edges are the suggestion, not the coarse list.
    expect(presetState(form)).toHaveTextContent("No preset pressed");
    expect(presetButton(form, "Coarse (quick check)")).toHaveAttribute("aria-pressed", "false");
    expect(edgeCount(form, "RPM edges (rpm)")).toBe(17);
    // The draws came back as a remembered value, marked; pressing Fine replaces it, visibly.
    expect(field(form, "Draws").value).toBe("200");
    expect(form.querySelector("#sim-rail-remembered")).not.toBeNull();
    const marked = () => form.querySelectorAll("[data-remembered]").length;
    const before = marked();
    await click(presetButton(form, "Fine"));
    expect(field(form, "Draws").value).toBe("1000");
    expect(marked()).toBe(before - 1);
  });

  it("is disabled, with the reason, for an image with no Fuel Comp breakpoints", async () => {
    route();
    window.history.replaceState(null, "", `/?rom=${SHA_TUNED}`);
    const tables = tablesWithAxes();
    routeControl({
      "GET /control/rom/library": { entries: [ENTRY_TUNED] },
      [`GET /control/rom/library/${SHA_TUNED}/tables`]: {
        ...tables,
        tables: tables.tables.filter((t) => t.kind !== "fuel_compensation"),
      },
    });
    renderRomEditor();
    await showRomView("Simulate");
    const form = screen.getByRole("form", { name: "Simulate form" });
    expect(presetButton(form, "Coarse (quick check)")).toBeDisabled();
    expect(presetButton(form, "Fine")).toBeDisabled();
    expect(form).toHaveTextContent(
      "This image gives no Fuel Comp breakpoints to build the edges from",
    );
  });
});

describe("the coarse-run banner on a returned run", () => {
  async function runAfter(setup: (form: HTMLElement) => Promise<void> | void) {
    route({ simulate: sizedAnswer });
    const form = await withPendingEdit();
    state(form);
    await setup(form);
    await click(runButton(form));
    await screen.findByRole("region", { name: "Simulation result" });
  }
  const banner = () => screen.queryByRole("note", { name: "Coarse run" });

  it("shows for the Coarse preset, with draws and points", async () => {
    await runAfter(async (form) => click(presetButton(form, "Coarse (quick check)")));
    expect(banner()).toHaveTextContent("200 draws × 64 points");
    expect(banner()).toHaveTextContent("64 points, below the Fine preset's 256");
    expect(banner()).toHaveTextContent("not unchanged");
  });

  it("shows for a hand-set low run that never touched a preset", async () => {
    await runAfter(() => {});
    // state() stated 200 draws on a 3 x 3 grid.
    expect(banner()).toHaveTextContent("200 draws × 9 points");
    expect(banner()).toHaveTextContent("below the Fine preset's 1,000");
  });

  it("shows when the draws are fine but the grid was cut down by hand", async () => {
    await runAfter(() => {
      type(screen.getByLabelText("Draws"), "1000");
    });
    expect(banner()).toHaveTextContent("1,000 draws × 9 points");
    expect(banner()).not.toHaveTextContent("draws, below");
  });

  it("does not show for the Fine preset", async () => {
    await runAfter(async (form) => click(presetButton(form, "Fine")));
    expect(screen.getByRole("region", { name: "Simulation result" })).toBeVisible();
    expect(banner()).not.toBeInTheDocument();
    expect(sentRequest()).toMatchObject({ draws: 1000 });
  });

  it("keeps every other request field and the 23-field shape under a preset", async () => {
    await runAfter(async (form) => click(presetButton(form, "Coarse (quick check)")));
    const request = sentRequest();
    expect(Object.keys(request).sort()).toEqual([...documentedSimFields()].sort());
    expect(request).toMatchObject({ draws: 200, baro_kpa: 80, seed: 20261015 });
    expect((request.rpm_edges as number[]).length).toBe(9);
  });
});

describe("the cost estimate", () => {
  it("says it is not yet measured, and offers the labelled laptop reference", async () => {
    route();
    const form = await withPendingEdit();
    state(form);
    type(field(form, "RPM edges (rpm)"), "400, 800");
    type(field(form, "Load edges (% fill)"), "10, 20, 30");
    type(field(form, "Draws"), "1000");
    const cost = costBlock(form);
    expect(cost).toHaveTextContent(
      "2 points × 1,000 draws = 2,000 point-draws. Not yet measured on this server.",
    );
    expect(cost).toHaveTextContent("Reference, not a prediction: the dev laptop took");
    expect(cost).toHaveTextContent("(W09.13)");
    expect(cost).toHaveTextContent("least squares on relative error");
    expect(cost).toHaveTextContent("Below the Fine preset");
  });

  it("is points x draws for the form's own grid, against the cap", async () => {
    route();
    const form = await withPendingEdit();
    await click(presetButton(form, "Fine"));
    expect(costBlock(form)).toHaveTextContent("256 points × 1,000 draws = 256,000 point-draws");
    expect(costBlock(form)).not.toHaveTextContent("Below the Fine preset");
    await click(presetButton(form, "Coarse (quick check)"));
    expect(costBlock(form)).toHaveTextContent("64 points × 200 draws = 12,800 point-draws");
    expect(costBlock(form)).toHaveTextContent("Below the Fine preset");
  });

  it("uses the rate from this server's earlier runs when there are some", async () => {
    window.localStorage.setItem(
      TIMINGS_KEY,
      JSON.stringify({
        version: 1,
        runs: [
          { points: 224, draws: 1000, seconds: 17 },
          { points: 224, draws: 1000, seconds: 17 },
        ],
      }),
    );
    route();
    const form = await withPendingEdit();
    await click(presetButton(form, "Fine"));
    const cost = costBlock(form);
    expect(cost).toHaveTextContent("from your last 2 runs on this server");
    expect(cost).not.toHaveTextContent("Not yet measured");
    expect(cost).toHaveTextContent("This server ran");
  });

  it("learns from a run's own elapsed time, and stores it under points x draws", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    let resolve: (value: RomSimResult) => void = () => {};
    route({ simulate: () => new Promise<RomSimResult>((done) => (resolve = done)) });
    const form = await withPendingEdit();
    state(form);
    await click(presetButton(form, "Coarse (quick check)"));
    expect(window.localStorage.getItem(TIMINGS_KEY)).toBeNull();
    await click(runButton(form));
    await act(async () => {
      vi.advanceTimersByTime(6000);
    });
    await act(async () => {
      resolve(buildSimResult());
    });
    const stored = JSON.parse(window.localStorage.getItem(TIMINGS_KEY) ?? "{}") as {
      runs: { points: number; draws: number; seconds: number }[];
    };
    expect(stored.runs).toEqual([{ points: 64, draws: 200, seconds: 6 }]);
    expect(costBlock(screen.getByRole("form", { name: "Simulate form" }))).toHaveTextContent(
      "from your last 1 run on this server",
    );
  });

  it("does not store a run that failed", async () => {
    route({
      simulate: new HttpError(
        400,
        JSON.stringify({ detail: { error: "sim_refused", reason: "x", message: "no" } }),
      ),
    });
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    await screen.findByRole("alert");
    expect(window.localStorage.getItem(TIMINGS_KEY)).toBeNull();
  });
});

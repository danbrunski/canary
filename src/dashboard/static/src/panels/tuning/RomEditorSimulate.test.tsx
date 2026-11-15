import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { HttpError, controlBlob } from "../../api/core";
import { downloadText } from "../../util/browser";
import { SIM_STORAGE_KEY } from "../../lib/romSim";
import {
  SIMULATE,
  STATED,
  route,
  runButton,
  sentRequest,
  setUpSimulateTests,
  simulateCalls,
  state,
  stateTiming,
  withPendingEdit,
  FUEL_TARGETS,
  wotNote,
  WOT_WARNING,
} from "../../test/romSimulateKit";
import { callsTo, click, renderRomEditor, type, type Answer } from "../../test/controlRoutes";
import { SHA_TUNED } from "../../test/romLibraryFixtures";
import { chooseRecent, showRomView } from "../../test/romEditorViews";
import { documentedSimFields } from "../../test/romSimDoc";
import { buildSimResult } from "../../test/romSimFixtures";
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
const blob = vi.mocked(controlBlob);

const save = vi.mocked(downloadText);

setUpSimulateTests();

describe("the Simulate form: off, empty, and honest about it", () => {
  it("is off for a base that is not a recorded revision, with the reason", async () => {
    route();
    renderRomEditor();
    await screen.findByRole("grid");
    await showRomView("Simulate");
    chooseRecent(/Edit from k106\.bin/);
    const note = await screen.findByRole("note");
    expect(note).toHaveTextContent(/Simulate is unavailable: base_not_a_recorded_revision\./);
    const form = screen.getByRole("form", { name: "Simulate form" });
    expect(runButton(form)).toBeDisabled();
    expect(within(form).getByLabelText("Baro (kPa)")).toBeDisabled();
  });

  it("is off while the preview has a refusal, with the reason", async () => {
    route({
      preview: new HttpError(
        400,
        JSON.stringify({
          detail: {
            error: "edit_refused",
            kind: "rom_effect",
            reason: "value_out_of_range",
            message: "raw 255 x 1.02 is 260",
          },
        }),
      ),
    });
    const form = await withPendingEdit();
    expect(within(form).queryByRole("note")).not.toBeInTheDocument();
    await showRomView("Edits & Preview");
    await click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByText("Refused: value_out_of_range");
    await showRomView("Simulate");
    expect(within(form).getByRole("note")).toHaveTextContent(
      /Simulate is unavailable: preview_refused\. The last preview was refused/,
    );
    state(form);
    expect(runButton(form)).toBeDisabled();
  });

  it("defaults nothing: no choice made, no value stated, Run names what is missing", async () => {
    route();
    const form = await withPendingEdit();
    for (const radio of within(form).getAllByRole("radio")) expect(radio).not.toBeChecked();
    for (const label of Object.keys(STATED).filter((key) => !/edges/.test(key))) {
      expect(within(form).getByLabelText(label)).toHaveValue("");
    }
    expect(within(form).getByLabelText("Rail pressure")).toHaveValue("");
    expect(runButton(form)).toBeDisabled();
    const missing = within(form).getByRole("status", { name: "What blocks Run" });
    for (const name of [
      "Grid",
      "Baro (kPa)",
      "IAT (°C)",
      "Coolant (°C)",
      "Battery (V)",
      "Throttle (%)",
      "Pedal flag (0-255)",
      "Rail pressure",
      "STFT (%)",
      "LTFT (%)",
      "Throttle closed learned (counts)",
      "Idle air learned (counts)",
      "EVAP purge area (counts)",
      "HI DET latch",
      "Knock control",
      "Candidate events",
      "Draws",
      "Seed",
    ]) {
      expect(missing).toHaveTextContent(name);
    }
    // The edit and the label are stated above, so they are not missing.
    expect(missing).not.toHaveTextContent("Edits");
    expect(missing).not.toHaveTextContent("Label");
    expect(simulateCalls()).toHaveLength(0);
  });

  it("shows the load basis as its one value with its meaning, and the draws bound", async () => {
    route();
    const form = await withPendingEdit();
    expect(form).toHaveTextContent("fill_fraction_as_manifold_ratio");
    expect(form).toHaveTextContent("(the only value)");
    expect(form).toHaveTextContent(/taken as MAP \/ baro/);
    expect(form).toHaveTextContent(/at most 2,000/);
    expect(within(form).queryByLabelText(/Load basis/)).not.toBeInTheDocument();
  });
});

describe("the grid edges", () => {
  it("are suggested from Fuel Comp's breakpoints, with the point count against the cap", async () => {
    route();
    const form = await withPendingEdit();
    const rpm = within(form).getByLabelText<HTMLInputElement>("RPM edges (rpm)").value.split(", ");
    expect(rpm).toHaveLength(17);
    expect(rpm.slice(0, 3)).toEqual(["200", "600", "1000"]);
    expect(rpm.at(-1)).toBe("6600");
    const load = within(form)
      .getByLabelText<HTMLInputElement>("Load edges (% fill)")
      .value.split(", ");
    expect(load).toHaveLength(17);
    expect(Number(load[0])).toBeCloseTo(3.125, 6);
    expect(form).toHaveTextContent("suggested from the base's Fuel Comp rpm breakpoints");
    expect(within(form).getByLabelText("Grid size")).toHaveTextContent(
      "16 × 16 = 256 of at most 1,024 cells",
    );
  });

  it("are the same whichever of rows or columns the payload calls rpm", async () => {
    route({ swap: true });
    const form = await withPendingEdit();
    expect(within(form).getByLabelText<HTMLInputElement>("RPM edges (rpm)").value).toMatch(
      /^200, 600, 1000,/,
    );
    expect(
      Number(
        within(form).getByLabelText<HTMLInputElement>("Load edges (% fill)").value.split(", ")[0],
      ),
    ).toBeCloseTo(3.125, 6);
  });

  it("are editable, and a load edge past the fill model's 100 % blocks Run until trimmed", async () => {
    route();
    const form = await withPendingEdit();
    state(form, ["RPM edges (rpm)", "Load edges (% fill)"]);
    // The suggestion's last edge is 103.125 %: the model spans 0-100 %.
    expect(runButton(form)).toBeDisabled();
    expect(within(form).getByRole("list", { name: "Values to fix" })).toHaveTextContent(
      /Load edges: edge 103\.125 is above 100 %.*load_outside_model/,
    );
    await click(within(form).getByRole("button", { name: "Trim load edges to 0–100 %" }));
    expect(runButton(form)).toBeEnabled();
    expect(within(form).getByLabelText<HTMLInputElement>("Load edges (% fill)").value).toMatch(
      /96\.875, 100$/,
    );
  });

  it("count the cells of what is typed and block a grid over the cap", async () => {
    route();
    const form = await withPendingEdit();
    state(form);
    expect(within(form).getByLabelText("Grid size")).toHaveTextContent(
      "3 × 3 = 9 of at most 1,024 cells",
    );
    const edges = Array.from({ length: 34 }, (_, i) => i).join(", ");
    type(within(form).getByLabelText("RPM edges (rpm)"), edges);
    type(within(form).getByLabelText("Load edges (% fill)"), edges);
    expect(within(form).getByLabelText("Grid size")).toHaveTextContent(
      "33 × 33 = 1089 of at most 1,024 cells",
    );
    expect(runButton(form)).toBeDisabled();
    expect(within(form).getByRole("list", { name: "Values to fix" })).toHaveTextContent(
      /grid_too_large/,
    );
    // Reset puts the base's breakpoints back.
    await click(within(form).getByRole("button", { name: "Reset to the base's breakpoints" }));
    expect(within(form).getByLabelText("Grid size")).toHaveTextContent("16 × 16 = 256");
  });
});

describe("the WOT column (pedal flag bit 0)", () => {
  it("says on the field what bit 0 does", async () => {
    route();
    const form = await withPendingEdit();
    expect(
      within(form).getByText(/Bit 0 forces the last Target AFR column \(the WOT column\)/),
    ).toBeVisible();
    expect(within(form).getByText(/no simulated point reads that column/)).toBeVisible();
  });

  it("warns before Run for a last-column Fuel Targets edit with the flag stated 0, and defaults nothing", async () => {
    route();
    const form = await withPendingEdit("wot richer", {
      table: FUEL_TARGETS,
      from: [3, 8],
      to: [4, 8],
    });
    state(form);
    expect(wotNote(form)).toHaveTextContent(WOT_WARNING);
    expect(runButton(form)).toBeEnabled();
    // Not blocking, and the field is still the operator's: it keeps the 0 they typed.
    expect(within(form).getByLabelText("Pedal flag (0-255)")).toHaveValue("0");
    await click(runButton(form));
    await screen.findByRole("region", { name: "Simulation result" });
    expect(sentRequest().pedal_flag).toBe(0);
    // And the result says the same, in its caveat block.
    expect(
      within(screen.getByRole("region", { name: "Read this first" })).getByText(
        "The WOT column was edited with pedal flag 0: this run cannot show that edit's effect.",
      ),
    ).toBeVisible();
  });

  it("is quiet for an empty flag, a flag with bit 0 set, and a middle column", async () => {
    route();
    const form = await withPendingEdit("wot richer", {
      table: FUEL_TARGETS,
      from: [3, 8],
      to: [4, 8],
    });
    state(form, ["Pedal flag (0-255)"]);
    expect(wotNote(form)).not.toBeInTheDocument();
    type(within(form).getByLabelText("Pedal flag (0-255)"), "1");
    expect(wotNote(form)).not.toBeInTheDocument();
    type(within(form).getByLabelText("Pedal flag (0-255)"), "3");
    expect(wotNote(form)).not.toBeInTheDocument();
    type(within(form).getByLabelText("Pedal flag (0-255)"), "2");
    expect(wotNote(form)).toHaveTextContent("With pedal flag 2 ");
    type(within(form).getByLabelText("Pedal flag (0-255)"), "1.5");
    expect(wotNote(form)).not.toBeInTheDocument();
  });

  it("is quiet for a Fuel Targets edit that stops short of the last column", async () => {
    route();
    const form = await withPendingEdit("mid", { table: FUEL_TARGETS, from: [3, 7], to: [4, 7] });
    state(form);
    expect(wotNote(form)).not.toBeInTheDocument();
  });

  it("is quiet for a Fuel Comp edit, and a result without a WOT edit has no WOT caveat", async () => {
    route();
    const form = await withPendingEdit();
    state(form);
    expect(wotNote(form)).not.toBeInTheDocument();
    await click(runButton(form));
    const caveats = await screen.findByRole("region", { name: "Read this first" });
    expect(caveats).not.toHaveTextContent("The WOT column was edited");
  });

  it("is quiet for a last-column Fuel Targets edit when the flag has bit 0 set, in the result too", async () => {
    route();
    const form = await withPendingEdit("wot", { table: FUEL_TARGETS, from: [3, 8], to: [3, 8] });
    state(form, ["Pedal flag (0-255)"]);
    type(within(form).getByLabelText("Pedal flag (0-255)"), "1");
    expect(wotNote(form)).not.toBeInTheDocument();
    await click(runButton(form));
    const caveats = await screen.findByRole("region", { name: "Read this first" });
    expect(caveats).not.toHaveTextContent("The WOT column was edited");
  });
});

describe("Run", () => {
  it("is blocked by one missing environment field, which it names, and sends nothing", async () => {
    route();
    const form = await withPendingEdit();
    state(form, ["Baro (kPa)"]);
    expect(runButton(form)).toBeDisabled();
    expect(within(form).getByRole("status", { name: "What blocks Run" })).toHaveTextContent(
      "Run needs: Baro (kPa).",
    );
    fireEvent.submit(form);
    expect(simulateCalls()).toHaveLength(0);
    type(within(form).getByLabelText("Baro (kPa)"), "80");
    expect(runButton(form)).toBeEnabled();
    expect(within(form).queryByRole("status", { name: "What blocks Run" })).not.toBeInTheDocument();
  });

  it("sends every one of the 23 fields the doc lists, as stated", async () => {
    route();
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    await screen.findByRole("region", { name: "Simulation result" });
    expect(simulateCalls()).toHaveLength(1);
    const call = simulateCalls()[0];
    expect(call.path).toBe(SIMULATE(SHA_TUNED));
    expect(call.method).toBe("POST");
    const request = sentRequest() as unknown as RomSimRequest;
    const documented = documentedSimFields();
    expect(documented).toHaveLength(23);
    expect(Object.keys(request).sort()).toEqual([...documented].sort());
    expect(request).toEqual({
      label: "idle richer",
      edits: [
        {
          table: "fuel_compensation",
          slot: 1,
          cells: [
            [0, 0],
            [0, 1],
          ],
          scale: 1.02,
          rounding: "nearest",
        },
      ],
      grid: "fuel_comp",
      rpm_edges: [200, 600, 1000, 1400],
      load_edges: [3.5, 10.7, 17.8, 25],
      load_basis: "fill_fraction_as_manifold_ratio",
      baro_kpa: 80,
      iat_c: 25,
      coolant_c: 88,
      battery_v: 14,
      rail_kpa: "working_assumption",
      throttle_pct: 20,
      pedal_flag: 0,
      stft_pct: 0,
      ltft_pct: 0,
      throttle_closed_learned: 0,
      idle_air_learned: 0,
      evap_purge_area: 0,
      high_det: "set",
      knock_control: "fallback",
      candidate_events: "ecu_reflash",
      draws: 200,
      seed: 20261015,
    });
  });

  it("sends a stated rail pressure as a number and the other grid and events as chosen", async () => {
    route();
    const form = await withPendingEdit();
    state(form);
    fireEvent.change(within(form).getByLabelText("Rail pressure"), { target: { value: "stated" } });
    expect(runButton(form)).toBeDisabled();
    expect(within(form).getByRole("status", { name: "What blocks Run" })).toHaveTextContent(
      "Rail pressure (kPa)",
    );
    type(within(form).getByLabelText("Rail pressure (kPa)"), "380");
    fireEvent.click(within(form).getByRole("radio", { name: /Fuel Targets axes/ }));
    fireEvent.click(within(form).getByRole("radio", { name: /Same learned state on both/ }));
    await click(runButton(form));
    await screen.findByRole("region", { name: "Simulation result" });
    expect(sentRequest()).toMatchObject({
      rail_kpa: 380,
      grid: "target_afr",
      candidate_events: "none",
    });
  });

  it("shows a spinner and the elapsed time while the one request is in flight", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    let resolve: (value: RomSimResult) => void = () => {};
    route({ simulate: () => new Promise<RomSimResult>((done) => (resolve = done)) });
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    const status = screen.getByText(/Simulating…/).closest("[role=status]") as HTMLElement;
    expect(status).toHaveTextContent("0 s elapsed");
    expect(runButton(form)).toBeDisabled();
    await act(async () => {
      vi.advanceTimersByTime(3200);
    });
    expect(status).toHaveTextContent("3 s elapsed");
    expect(simulateCalls()).toHaveLength(1);
    await act(async () => {
      resolve(buildSimResult());
    });
    expect(screen.queryByText(/Simulating…/)).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Simulation result" })).toBeVisible();
  });

  it("puts the caveat block before the first map, and offers the result as JSON, not an image", async () => {
    const result = buildSimResult();
    route({ simulate: result });
    const form = await withPendingEdit("light load");
    state(form);
    await click(runButton(form));
    const section = await screen.findByRole("region", { name: "Simulation result" });
    const caveats = within(section).getByRole("region", { name: "Read this first" });
    const firstMap = within(section).getAllByRole("figure")[0];
    expect(
      caveats.compareDocumentPosition(firstMap) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // The edits listed are the ones that were sent.
    expect(within(caveats).getByRole("list", { name: "Edit list" })).toHaveTextContent(
      /Fuel Compensation: scale raw ×1\.02 \(\+2 %, nearest\) on 2 cells/,
    );

    await click(screen.getByRole("button", { name: "Download result JSON" }));
    expect(save).toHaveBeenCalledTimes(1);
    const [content, name, mime] = save.mock.calls[0];
    expect(JSON.parse(content)).toEqual(result);
    expect(name).toBe("light_load.sim-result.json");
    expect(mime).toBe("application/json");
    expect(blob).not.toHaveBeenCalled();
    expect(callsTo(/\/download$/)).toHaveLength(0);
    expect(document.querySelector("a[href], a[download]")).toBeNull();
    const downloads = screen
      .getAllByRole("button")
      .map((button) => button.textContent ?? "")
      .filter((text) => /download/i.test(text));
    expect(downloads).toEqual(["Download result JSON"]);
  });

  it("notes when the pending edits change after a run", async () => {
    route();
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    await screen.findByRole("region", { name: "Simulation result" });
    expect(screen.queryByText(/pending edits have changed since this run/)).not.toBeInTheDocument();
    await showRomView("Edits & Preview");
    await click(screen.getByRole("button", { name: "Remove edit 1" }));
    await showRomView("Simulate");
    // The view's label says so too, before anyone opens it.
    expect(screen.getByRole("button", { name: "Simulate · stale" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(await screen.findByText(/pending edits have changed since this run/)).toBeVisible();
  });
});

describe("a run that does not return a result says why", () => {
  async function runWith(simulate: Answer) {
    route({ simulate });
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    return screen.findByRole("alert");
  }

  it("409 analysis_deferred: a capture is recording, take the images to the laptop", async () => {
    const alert = await runWith(
      new HttpError(
        409,
        JSON.stringify({
          detail: {
            error: "analysis_deferred",
            reason: "capture_active",
            profile: "tuning",
            message: "a capture (tuning) is recording; analysis would load the Pi",
            laptop_path: "Download the image from the ROM Library and run the spec on the laptop.",
          },
        }),
      ),
    );
    expect(alert).toHaveTextContent("Deferred: a capture is recording: capture_active");
    expect(alert).toHaveTextContent(/Pull the images and run the edit spec on the laptop/);
    expect(alert).toHaveTextContent("a capture (tuning) is recording; analysis would load the Pi");
    expect(alert).toHaveTextContent(
      "Download the image from the ROM Library and run the spec on the laptop.",
    );
    expect(screen.queryByRole("region", { name: "Simulation result" })).not.toBeInTheDocument();
    expect(runButton(screen.getByRole("form", { name: "Simulate form" }))).toBeEnabled();
  });

  it("409 analysis_busy", async () => {
    const alert = await runWith(
      new HttpError(
        409,
        JSON.stringify({
          detail: {
            error: "analysis_busy",
            reason: "child_busy",
            message: "another analysis is running",
          },
        }),
      ),
    );
    expect(alert).toHaveTextContent("Busy: another analysis is running: child_busy");
    expect(alert).toHaveTextContent("another analysis is running");
  });

  it("400 sim_refused: the reason and the class", async () => {
    const alert = await runWith(
      new HttpError(
        400,
        JSON.stringify({
          detail: {
            error: "sim_refused",
            kind: "sim_design",
            reason: "load_outside_model",
            message: "load edges 3.5..120 %: the fill model spans 0-100 %",
          },
        }),
      ),
    );
    expect(alert).toHaveTextContent("Refused: load_outside_model");
    expect(alert).toHaveTextContent("load edges 3.5..120 %: the fill model spans 0-100 %");
    expect(alert).toHaveTextContent("refusal class: sim_design");
  });

  it("503 analysis_unavailable: the child did not finish", async () => {
    const alert = await runWith(
      new HttpError(
        503,
        JSON.stringify({
          detail: {
            error: "analysis_unavailable",
            reason: "child_timed_out",
            message: "the analysis did not finish in 900 s",
          },
        }),
      ),
    );
    expect(alert).toHaveTextContent("Unavailable: the job did not finish: child_timed_out");
    expect(alert).toHaveTextContent("the analysis did not finish in 900 s");
  });

  it("422: pydantic's list, by field", async () => {
    const alert = await runWith(
      new HttpError(
        422,
        JSON.stringify({
          detail: [{ loc: ["body", "baro_kpa"], msg: "Field required", type: "missing" }],
        }),
      ),
    );
    expect(alert).toHaveTextContent("Request rejected: validation");
    expect(alert).toHaveTextContent("baro_kpa: Field required");
  });
});

describe("remembered values", () => {
  async function runOnce() {
    route();
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    await screen.findByRole("region", { name: "Simulation result" });
  }

  it("come back visibly marked, are sent only as shown, and exclude the grid, events and edges", async () => {
    await runOnce();
    const stored = JSON.parse(window.localStorage.getItem(SIM_STORAGE_KEY) ?? "{}") as {
      values: Record<string, string>;
    };
    expect(stored.values).toMatchObject({ baro: "80", draws: "200", seed: "20261015" });
    expect(Object.keys(stored.values)).not.toContain("candidateEvents");
    expect(Object.keys(stored.values)).not.toContain("grid");

    cleanup();
    vi.clearAllMocks();
    const form = await withPendingEdit("again");
    expect(within(form).getByLabelText("Baro (kPa)")).toHaveValue("80");
    expect(within(form).getByLabelText("Draws")).toHaveValue("200");
    expect(form).toHaveTextContent(/14 values were remembered from your last run/);
    expect(form.querySelectorAll("[data-remembered]").length).toBe(14);
    // Not remembered: the grid and the candidate events are chosen every time.
    for (const radio of within(form).getAllByRole("radio")) expect(radio).not.toBeChecked();
    expect(runButton(form)).toBeDisabled();
    expect(within(form).getByRole("status", { name: "What blocks Run" })).toHaveTextContent(
      "Run needs: Grid, HI DET latch, Knock control, Candidate events.",
    );
    // Choose them, and what is sent is exactly what the form showed.
    fireEvent.click(within(form).getByRole("radio", { name: /Fuel Comp axes/ }));
    fireEvent.click(within(form).getByRole("radio", { name: /Same learned state on both/ }));
    stateTiming(form);
    type(within(form).getByLabelText("RPM edges (rpm)"), "200, 600, 1000, 1400");
    type(within(form).getByLabelText("Load edges (% fill)"), "3.5, 10.7, 17.8, 25");
    await click(runButton(form));
    await screen.findByRole("region", { name: "Simulation result" });
    expect(sentRequest()).toMatchObject({
      baro_kpa: 80,
      draws: 200,
      seed: 20261015,
      candidate_events: "none",
    });
  });

  it("lose their mark when edited, and are forgotten on request", async () => {
    await runOnce();
    cleanup();
    const form = await withPendingEdit();
    const baro = within(form).getByLabelText("Baro (kPa)");
    expect(baro).toHaveAttribute("aria-describedby");
    type(baro, "82");
    expect(baro).not.toHaveAttribute("aria-describedby");
    expect(form).toHaveTextContent(/13 values were remembered/);
    await click(within(form).getByRole("button", { name: "Clear remembered values" }));
    expect(within(form).getByLabelText("Draws")).toHaveValue("");
    expect(form.querySelectorAll("[data-remembered]")).toHaveLength(0);
    expect(window.localStorage.getItem(SIM_STORAGE_KEY)).toBeNull();
  });

  it("do not break the form when storage is blocked", async () => {
    const get = Storage.prototype.getItem;
    const set = Storage.prototype.setItem;
    const remove = Storage.prototype.removeItem;
    Storage.prototype.getItem = () => {
      throw new Error("blocked");
    };
    Storage.prototype.setItem = () => {
      throw new Error("blocked");
    };
    Storage.prototype.removeItem = () => {
      throw new Error("blocked");
    };
    try {
      route();
      const form = await withPendingEdit();
      state(form);
      await click(runButton(form));
      await screen.findByRole("region", { name: "Simulation result" });
      expect(simulateCalls()).toHaveLength(1);
    } finally {
      Storage.prototype.getItem = get;
      Storage.prototype.setItem = set;
      Storage.prototype.removeItem = remove;
    }
  });
});

describe("the seed", () => {
  it("is filled in visibly by New seed, never sent unseen", async () => {
    route();
    const form = await withPendingEdit();
    state(form, ["Seed"]);
    expect(runButton(form)).toBeDisabled();
    await click(within(form).getByRole("button", { name: "New seed" }));
    const seed = within(form).getByLabelText<HTMLInputElement>("Seed");
    expect(seed.value).toMatch(/^\d+$/);
    expect(runButton(form)).toBeEnabled();
    await click(runButton(form));
    await waitFor(() => expect(simulateCalls()).toHaveLength(1));
    expect(sentRequest().seed).toBe(Number(seed.value));
  });
});

describe("Simulate as a view of its own (W10.9)", () => {
  const simulateView = () =>
    within(screen.getByRole("group", { name: "ROM Editor views" }))
      .getAllByRole("button")
      .find((button) => button.textContent?.startsWith("Simulate"));

  it("keeps a typed field across a view switch", async () => {
    route();
    const form = await withPendingEdit();
    type(within(form).getByLabelText("Baro (kPa)"), "71.5");
    type(within(form).getByLabelText("RPM edges (rpm)"), "800, 1600, 2400");
    await showRomView("Tables");
    await showRomView("Edits & Preview");
    await showRomView("Simulate");
    // Queried afresh: a remounted form would be a new, empty one.
    const again = screen.getByRole("form", { name: "Simulate form" });
    expect(again).toBe(form);
    expect(within(again).getByLabelText("Baro (kPa)")).toHaveValue("71.5");
    expect(within(again).getByLabelText("RPM edges (rpm)")).toHaveValue("800, 1600, 2400");
  });

  it("says a run is in flight on the label, and lands a run left while running", async () => {
    let resolve: (value: RomSimResult) => void = () => {};
    route({ simulate: () => new Promise<RomSimResult>((done) => (resolve = done)) });
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    expect(simulateView()).toHaveTextContent("Simulate · running");
    await showRomView("Tables");
    expect(simulateView()).toHaveTextContent("Simulate · running");
    await act(async () => {
      resolve(buildSimResult());
    });
    // The run is the editor's, not the view's: it landed while Tables showed.
    expect(simulateView()).toHaveTextContent(/^Simulate$/);
    await showRomView("Simulate");
    expect(screen.getByRole("region", { name: "Simulation result" })).toBeVisible();
    expect(simulateCalls()).toHaveLength(1);
  });

  it("says a run failed on the label", async () => {
    route({
      simulate: new HttpError(
        503,
        JSON.stringify({ detail: { error: "analysis_unavailable", message: "child died" } }),
      ),
    });
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    await screen.findByRole("alert");
    expect(simulateView()).toHaveTextContent("Simulate · failed");
  });
});

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlBlob, controlJson } from "../../api/core";
import { SIM_STORAGE_KEY } from "../../lib/romSim";
import { ENTRY_K106, ENTRY_TUNED, SHA_K106, SHA_TUNED } from "../../test/romLibraryFixtures";
import { documentedSimFields } from "../../test/romSimDoc";
import { buildSimResult, tablesWithAxes } from "../../test/romSimFixtures";
import type { RomSimRequest, RomSimResult } from "../../types/romEditor";
import { downloadText } from "../../util/browser";
import { RomEditorPage } from "./RomEditorPage";

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
const control = vi.mocked(controlJson);
const blob = vi.mocked(controlBlob);
const save = vi.mocked(downloadText);

const click = async (element: HTMLElement) => {
  await act(async () => {
    fireEvent.click(element);
  });
};
const type = (element: HTMLElement, text: string) =>
  fireEvent.change(element, { target: { value: text } });
const bodyOf = (init?: RequestInit) => (typeof init?.body === "string" ? init.body : "");

const SIMULATE = (sha: string) => `/control/rom/library/${sha}/simulate`;
const PREVIEW = (sha: string) => `/control/rom/library/${sha}/edits/preview`;

type Answer = unknown;

function route(options: { simulate?: Answer; preview?: Answer; swap?: boolean } = {}) {
  const tables = tablesWithAxes(options.swap ?? false);
  const table: Record<string, Answer> = {
    "GET /control/rom/library": { entries: [ENTRY_TUNED, ENTRY_K106] },
    [`GET /control/rom/library/${SHA_TUNED}/tables`]: tables,
    [`GET /control/rom/library/${SHA_K106}/tables`]: { ...tables, sha256: SHA_K106 },
    [`POST ${SIMULATE(SHA_TUNED)}`]: options.simulate ?? (() => buildSimResult()),
    [`POST ${PREVIEW(SHA_TUNED)}`]:
      options.preview ??
      (() => {
        throw new Error("no preview expected");
      }),
  };
  control.mockImplementation((path: string, _token: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${path}`;
    if (!(key in table)) return Promise.reject(new Error(`unexpected ${key}`));
    try {
      let value = table[key];
      if (typeof value === "function") value = (value as (init?: RequestInit) => unknown)(init);
      return value instanceof Error ? Promise.reject(value) : Promise.resolve(value as never);
    } catch (err) {
      return Promise.reject(err instanceof Error ? err : new Error(String(err)));
    }
  });
}

const simulateCalls = () =>
  control.mock.calls.filter(([path]) => String(path).endsWith("/simulate"));
const sentRequest = (): Record<string, unknown> =>
  JSON.parse(bodyOf(simulateCalls()[0]?.[2])) as Record<string, unknown>;

// Each of these renders the whole page and fills a 20-field form: on a loaded
// machine that is slow, and slow is not wrong.
vi.setConfig({ testTimeout: 30_000 });

beforeEach(() => window.localStorage.clear());
afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
});

/** The editor starts unloaded; the `?rom=` link loads the recorded revision. */
function renderEditor() {
  window.history.replaceState(null, "", `/?rom=${SHA_TUNED}`);
  return render(<RomEditorPage token="t" status={null} />);
}

function cell(grid: HTMLElement, row: number, col: number) {
  return within(grid).getByRole("gridcell", { name: new RegExp(`^Row ${row}, column ${col}:`) });
}

/** Open the page, pick Fuel Comp, scale cells (1,1)-(1,2) by 2 % and name the set. */
async function withPendingEdit(
  label = "idle richer",
  target: { table: string; from: [number, number]; to: [number, number] } = {
    table: "Fuel Compensation (Map 1)",
    from: [1, 1],
    to: [1, 2],
  },
) {
  renderEditor();
  fireEvent.click(await screen.findByRole("button", { name: target.table }));
  const grid = screen.getByRole("grid", { name: `${target.table} cells` });
  fireEvent.mouseDown(cell(grid, ...target.from));
  fireEvent.mouseDown(cell(grid, ...target.to), { shiftKey: true });
  fireEvent.mouseUp(window);
  const operation = screen.getByRole("form", { name: "Operation" });
  fireEvent.change(within(operation).getByLabelText("Operation"), { target: { value: "scale" } });
  type(within(operation).getByLabelText("Scale (%)"), "2");
  fireEvent.change(within(operation).getByLabelText("Rounding"), { target: { value: "nearest" } });
  await click(within(operation).getByRole("button", { name: "Apply to selection" }));
  type(screen.getByLabelText("Label for this edit set"), label);
  return screen.getByRole("form", { name: "Simulate form" });
}

const STATED: Record<string, string> = {
  "RPM edges (rpm)": "200, 600, 1000, 1400",
  "Load edges (% fill)": "3.5, 10.7, 17.8, 25",
  "Baro (kPa)": "80",
  "IAT (°C)": "25",
  "Coolant (°C)": "88",
  "Battery (V)": "14",
  "Throttle (%)": "20",
  "Pedal flag (0-255)": "0",
  "STFT (%)": "0",
  "LTFT (%)": "0",
  "Throttle closed learned (counts)": "0",
  "Idle air learned (counts)": "0",
  "EVAP purge area (counts)": "0",
  Draws: "200",
  Seed: "20261015",
};

/** State every field of the form, as an operator would. `skip` leaves labelled fields alone. */
function state(form: HTMLElement, skip: string[] = []) {
  for (const [label, value] of Object.entries(STATED)) {
    if (!skip.includes(label)) type(within(form).getByLabelText(label), value);
  }
  fireEvent.click(within(form).getByRole("radio", { name: /Fuel Comp axes/ }));
  fireEvent.change(within(form).getByLabelText("Rail pressure"), {
    target: { value: "working_assumption" },
  });
  fireEvent.click(within(form).getByRole("radio", { name: /Fresh flash resets trims/ }));
}

const runButton = (form: HTMLElement) =>
  within(form).getByRole("button", { name: "Run simulation" });

describe("the Simulate form: off, empty, and honest about it", () => {
  it("is off for a base that is not a recorded revision, with the reason", async () => {
    route();
    renderEditor();
    await screen.findByRole("grid");
    fireEvent.click(screen.getByRole("button", { name: /Edit from k106\.bin/ }));
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
    await click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByText("Refused: value_out_of_range");
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

const FUEL_TARGETS = "Fuel Targets (Map 1)";
const wotNote = (form: HTMLElement) =>
  within(form).queryByRole("note", { name: "WOT column warning" });
const WOT_WARNING =
  "Your edit touches the WOT column. With pedal flag 0 the simulated points never read it, " +
  "so this run will show no change from this edit. Set pedal flag to 1 (bit 0) to simulate " +
  "wide-open throttle.";

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

  it("sends every one of the 21 fields the doc lists, as stated", async () => {
    route();
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    await screen.findByRole("region", { name: "Simulation result" });
    expect(simulateCalls()).toHaveLength(1);
    const call = simulateCalls()[0];
    expect(call[0]).toBe(SIMULATE(SHA_TUNED));
    expect(call[2]?.method).toBe("POST");
    const request = sentRequest() as unknown as RomSimRequest;
    const documented = documentedSimFields();
    expect(documented).toHaveLength(21);
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
    expect(control.mock.calls.some(([path]) => String(path).endsWith("/download"))).toBe(false);
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
    await click(screen.getByRole("button", { name: "Remove edit 1" }));
    expect(await screen.findByText(/pending edits have changed since this run/)).toBeVisible();
  });
});

describe("a run that does not return a result says why", () => {
  async function runWith(simulate: unknown) {
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
      "Run needs: Grid, Candidate events.",
    );
    // Choose them, and what is sent is exactly what the form showed.
    fireEvent.click(within(form).getByRole("radio", { name: /Fuel Comp axes/ }));
    fireEvent.click(within(form).getByRole("radio", { name: /Same learned state on both/ }));
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

// -- W09.9: coarse and fine, the cost estimate, Cancel -------------------------------------

const TIMINGS_KEY = "canary.romSim.timings";
const field = (form: HTMLElement, label: string) =>
  within(form).getByLabelText<HTMLInputElement>(label);
const edgeCount = (form: HTMLElement, label: string) => field(form, label).value.split(", ").length;
const presetButton = (form: HTMLElement, name: string) =>
  within(form).getByRole("button", { name });
const presetState = (form: HTMLElement) =>
  within(form).getByRole("status", { name: "Preset state" });
const costBlock = (form: HTMLElement) => within(form).getByRole("group", { name: "Cost estimate" });

/** A simulate answer sized from what the request asked for, as the daemon's would be. */
function sizedAnswer(init?: RequestInit): RomSimResult {
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
    control.mockImplementation((path: string, _token: string, init?: RequestInit) => {
      const key = `${init?.method ?? "GET"} ${path}`;
      if (key === "GET /control/rom/library")
        return Promise.resolve({ entries: [ENTRY_TUNED] } as never);
      if (key === `GET /control/rom/library/${SHA_TUNED}/tables`) {
        return Promise.resolve({
          ...tables,
          tables: tables.tables.filter((t) => t.kind !== "fuel_compensation"),
        } as never);
      }
      return Promise.reject(new Error(`unexpected ${key}`));
    });
    render(<RomEditorPage token="t" status={null} />);
    const form = await screen.findByRole("form", { name: "Simulate form" });
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

  it("keeps every other request field and the 21-field shape under a preset", async () => {
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
    expect(cost).toHaveTextContent("(W09.6)");
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

describe("Cancel", () => {
  const busy = () =>
    new HttpError(
      409,
      JSON.stringify({
        detail: {
          error: "analysis_busy",
          reason: "child_busy",
          message: "another analysis is running",
        },
      }),
    );

  it("stops waiting, aborts the request, and says the server finishes the run", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    route({ simulate: () => new Promise<RomSimResult>(() => {}) });
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    const running = screen.getByText(/Simulating…/).closest("[role=status]") as HTMLElement;
    // Before anyone clicks: the button and what it does.
    expect(within(running).getByRole("button", { name: "Cancel (stop waiting)" })).toBeVisible();
    expect(running).toHaveTextContent(
      /Cancel only stops waiting: the server finishes this run \(about [\d.]+ s, the dev-laptop fit; not yet measured on this server\) before another can start/,
    );
    expect(running).toHaveTextContent("No route stops it sooner");
    const signal = simulateCalls()[0][2]?.signal as AbortSignal;
    expect(signal.aborted).toBe(false);
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    await click(within(running).getByRole("button", { name: "Cancel (stop waiting)" }));
    expect(signal.aborted).toBe(true);
    expect(screen.queryByText(/Simulating…/)).not.toBeInTheDocument();
    const stopped = screen
      .getByText(/You stopped waiting after 2 s/)
      .closest("[role=status]") as HTMLElement;
    expect(stopped).toHaveTextContent("The daemon is still running that simulation");
    expect(stopped).toHaveTextContent(
      "a different simulation is refused as busy (409 analysis_busy)",
    );
    expect(stopped).toHaveTextContent("re-attaches to the job already running");
    // No result, no timing stored from a run nobody saw finish.
    expect(screen.queryByRole("region", { name: "Simulation result" })).not.toBeInTheDocument();
    expect(window.localStorage.getItem(TIMINGS_KEY)).toBeNull();
    expect(runButton(form)).toBeEnabled();
  });

  it("explains the busy 409 that follows as the run that was cancelled", async () => {
    let calls = 0;
    route({
      simulate: () => {
        calls += 1;
        return calls === 1 ? new Promise<RomSimResult>(() => {}) : busy();
      },
    });
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    await click(screen.getByRole("button", { name: "Cancel (stop waiting)" }));
    // A different request: another seed.
    type(field(form, "Seed"), "42");
    await click(runButton(form));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Busy: another analysis is running: child_busy");
    expect(alert).toHaveTextContent(
      "That is probably the run you cancelled: Cancel stops waiting, the daemon kept running it.",
    );
    expect(simulateCalls()).toHaveLength(2);
  });

  it("does not claim a busy 409 is the cancelled run when nothing was cancelled", async () => {
    route({ simulate: busy() });
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent("run you cancelled");
  });

  it("never shows the result of a run that was cancelled, even if it comes back", async () => {
    let resolve: (value: RomSimResult) => void = () => {};
    route({ simulate: () => new Promise<RomSimResult>((done) => (resolve = done)) });
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    await click(screen.getByRole("button", { name: "Cancel (stop waiting)" }));
    await act(async () => {
      resolve(buildSimResult());
    });
    expect(screen.queryByRole("region", { name: "Simulation result" })).not.toBeInTheDocument();
    expect(screen.getByText(/You stopped waiting/)).toBeVisible();
  });
});

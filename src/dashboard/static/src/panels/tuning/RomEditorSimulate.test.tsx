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
async function withPendingEdit(label = "idle richer") {
  renderEditor();
  fireEvent.click(await screen.findByRole("button", { name: "Fuel Compensation (Map 1)" }));
  const grid = screen.getByRole("grid", { name: "Fuel Compensation (Map 1) cells" });
  fireEvent.mouseDown(cell(grid, 1, 1));
  fireEvent.mouseDown(cell(grid, 1, 2), { shiftKey: true });
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

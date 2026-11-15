import { fireEvent, screen, within } from "@testing-library/react";
import { beforeEach, vi } from "vitest";

import { ENTRY_K106, ENTRY_TUNED, SHA_K106, SHA_TUNED } from "./romLibraryFixtures";
import { cell, findTreeTable, showRomView } from "./romEditorViews";
import { buildSimResult, tablesWithAxes } from "./romSimFixtures";
import {
  callsTo,
  click,
  type,
  jsonOf,
  renderRomEditor,
  resetControlRoutesAfterEach,
  routeControl,
  type Answer,
} from "./controlRoutes";

// What the Simulate tests share (R28.4): the routes the editor reads, the form
// helpers, and the page-level set-up. The module mock of `api/core` stays in
// each test file (see `controlRoutes.ts`).

export const TYPICAL = `/control/rom/editor/typical-values?sha=${SHA_TUNED}`;
export const SIMULATE = (sha: string) => `/control/rom/library/${sha}/simulate`;
export const PREVIEW = (sha: string) => `/control/rom/library/${sha}/edits/preview`;

/** What the page reads, and what each of its writes answers; an unset answer
 *  that the page asks for rejects the test with a named message. */
export function route(
  options: { simulate?: Answer; preview?: Answer; swap?: boolean; typical?: Answer } = {},
) {
  const tables = tablesWithAxes(options.swap ?? false);
  routeControl({
    "GET /control/rom/library": { entries: [ENTRY_TUNED, ENTRY_K106] },
    [`GET /control/rom/library/${SHA_TUNED}/tables`]: tables,
    [`GET /control/rom/library/${SHA_K106}/tables`]: { ...tables, sha256: SHA_K106 },
    [`GET ${TYPICAL}`]:
      options.typical ??
      (() => {
        throw new Error("no typical values expected");
      }),
    [`POST ${SIMULATE(SHA_TUNED)}`]: options.simulate ?? (() => buildSimResult()),
    [`POST ${PREVIEW(SHA_TUNED)}`]:
      options.preview ??
      (() => {
        throw new Error("no preview expected");
      }),
  });
}

export const simulateCalls = () => callsTo(/\/simulate$/);
export const sentRequest = (): Record<string, unknown> =>
  jsonOf<Record<string, unknown>>(simulateCalls()[0]?.init);

/** Each of these renders the whole page and fills a 20-field form: on a loaded
 *  machine that is slow, and slow is not wrong. Call it once at the top of a
 *  Simulate test file. */
export function setUpSimulateTests(): void {
  vi.setConfig({ testTimeout: 30_000 });
  beforeEach(() => window.localStorage.clear());
  resetControlRoutesAfterEach();
}

/** Open the page, pick Fuel Comp, scale cells (1,1)-(1,2) by 2 % and name the set. */
export async function withPendingEdit(
  label = "idle richer",
  target: { table: string; from: [number, number]; to: [number, number] } = {
    table: "Fuel Compensation (Map 1)",
    from: [1, 1],
    to: [1, 2],
  },
) {
  renderRomEditor();
  fireEvent.click(await findTreeTable(target.table));
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
  await showRomView("Simulate");
  return screen.getByRole("form", { name: "Simulate form" });
}

export const STATED: Record<string, string> = {
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
export function state(form: HTMLElement, skip: string[] = []) {
  for (const [label, value] of Object.entries(STATED)) {
    if (!skip.includes(label)) type(within(form).getByLabelText(label), value);
  }
  fireEvent.click(within(form).getByRole("radio", { name: /Fuel Comp axes/ }));
  fireEvent.change(within(form).getByLabelText("Rail pressure"), {
    target: { value: "working_assumption" },
  });
  fireEvent.click(within(form).getByRole("radio", { name: /Fresh flash resets trims/ }));
  stateTiming(form);
}

/** State the ignition chain's two ECU states, as an operator would (no default exists). */
export function stateTiming(form: HTMLElement, highDet = /^Set\b/, knock = /^Fallback\b/) {
  // Knock control first: with fallback chosen the latch's "clear" is unavailable (an
  // operator who wants clear / active picks active first).
  fireEvent.click(
    within(within(form).getByRole("group", { name: "Knock control" })).getByRole("radio", {
      name: knock,
    }),
  );
  fireEvent.click(
    within(within(form).getByRole("group", { name: "HI DET latch" })).getByRole("radio", {
      name: highDet,
    }),
  );
}

export const runButton = (form: HTMLElement) =>
  within(form).getByRole("button", { name: "Run simulation" });

export const TIMINGS_KEY = "canary.romSim.timings";
export const field = (form: HTMLElement, label: string) =>
  within(form).getByLabelText<HTMLInputElement>(label);
export const edgeCount = (form: HTMLElement, label: string) =>
  field(form, label).value.split(", ").length;
export const presetButton = (form: HTMLElement, name: string) =>
  within(form).getByRole("button", { name });
export const presetState = (form: HTMLElement) =>
  within(form).getByRole("status", { name: "Preset state" });
export const costBlock = (form: HTMLElement) =>
  within(form).getByRole("group", { name: "Cost estimate" });

export const FUEL_TARGETS = "Fuel Targets (Map 1)";
export const wotNote = (form: HTMLElement) =>
  within(form).queryByRole("note", { name: "WOT column warning" });
export const WOT_WARNING =
  "Your edit touches the WOT column. With pedal flag 0 the simulated points never read it, " +
  "so this run will show no change from this edit. Set pedal flag to 1 (bit 0) to simulate " +
  "wide-open throttle.";

import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SIM_STORAGE_KEY } from "../../lib/romSim";
import {
  route,
  runButton,
  sentRequest,
  setUpSimulateTests,
  state,
  stateTiming,
  withPendingEdit,
} from "../../test/romSimulateKit";
import { click, renderRomEditor, type } from "../../test/controlRoutes";
import { cell, findTreeTable, showRomView } from "../../test/romEditorViews";
import { buildSimResult, buildTimingBlock } from "../../test/romSimFixtures";

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

describe("the ignition timing state and the timing trims (E09.7)", () => {
  const TRIM_B = "Ignition timing trim B (Map 1)";
  const group = (form: HTMLElement, name: string) => within(form).getByRole("group", { name });

  it("offers the HI DET latch and knock control unchosen, with the facts that make them a choice", async () => {
    route();
    const form = await withPendingEdit();
    for (const name of ["HI DET latch", "Knock control"]) {
      const radios = within(group(form, name)).getAllByRole("radio");
      expect(radios).toHaveLength(2);
      for (const radio of radios) expect(radio).not.toBeChecked();
      expect(group(form, name)).toHaveTextContent("Choose one: there is no default.");
    }
    const state = within(form).getByRole("group", { name: "Ignition timing state" });
    expect(state).toHaveTextContent(/2026-10-08 the truck read the latch set/);
    expect(state).toHaveTextContent(/no default, no typical value, and are not remembered/);
    expect(within(form).getByRole("status", { name: "What blocks Run" })).toHaveTextContent(
      /HI DET latch, Knock control/,
    );
  });

  it("blocks Run on one missing, naming it, and sends the pairing chosen, both ways", async () => {
    route();
    const form = await withPendingEdit();
    state(form, []);
    // state() chose set / fallback; the other pairing is the operator's to pick instead.
    stateTiming(form, /^Clear\b/, /^Active\b/);
    expect(
      within(group(form, "HI DET latch")).getByRole("radio", { name: /^Clear\b/ }),
    ).toBeChecked();
    await click(runButton(form));
    await screen.findByRole("region", { name: "Simulation result" });
    expect(sentRequest()).toMatchObject({ high_det: "clear", knock_control: "active" });
  });

  it("makes fallback with the latch clear impossible to submit, and says why", async () => {
    route();
    const form = await withPendingEdit();
    state(form, []);
    const latch = () => group(form, "HI DET latch");
    const knock = () => group(form, "Knock control");
    const clear = () => within(latch()).getByRole("radio", { name: /^Clear\b/ });
    // state() chose set / fallback: clear is off, with the one-line why.
    expect(clear()).toBeDisabled();
    expect(latch()).toHaveTextContent(/Not available with knock control in fallback/);
    expect(within(latch()).getByRole("radio", { name: /^Set\b/ })).toBeChecked();
    // Active makes clear available again.
    fireEvent.click(within(knock()).getByRole("radio", { name: /^Active\b/ }));
    expect(clear()).toBeEnabled();
    expect(latch()).not.toHaveTextContent(/Not available with knock control in fallback/);
    fireEvent.click(clear());
    expect(clear()).toBeChecked();
    // Choosing fallback with clear chosen moves the latch to set rather than keep the pairing.
    fireEvent.click(within(knock()).getByRole("radio", { name: /^Fallback\b/ }));
    expect(within(latch()).getByRole("radio", { name: /^Set\b/ })).toBeChecked();
    expect(clear()).not.toBeChecked();
    expect(clear()).toBeDisabled();
    await click(runButton(form));
    await screen.findByRole("region", { name: "Simulation result" });
    expect(sentRequest()).toMatchObject({ high_det: "set", knock_control: "fallback" });
  });

  it("remembers neither across a visit, whatever the last run chose", async () => {
    route();
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    await screen.findByRole("region", { name: "Simulation result" });
    const stored = JSON.parse(window.localStorage.getItem(SIM_STORAGE_KEY) ?? "{}") as {
      values: Record<string, string>;
    };
    expect(Object.keys(stored.values)).not.toContain("highDet");
    expect(Object.keys(stored.values)).not.toContain("knockControl");
    cleanup();
    vi.clearAllMocks();
    const again = await withPendingEdit("again");
    for (const radio of within(again).getAllByRole("radio")) expect(radio).not.toBeChecked();
    expect(within(again).getByRole("status", { name: "What blocks Run" })).toHaveTextContent(
      /HI DET latch, Knock control/,
    );
  });

  it("edits a timing trim like a fuel table: a masked grid, the flag kept, the request exact", async () => {
    route({ simulate: { ...buildSimResult(), ignition_timing: buildTimingBlock("trim_b") } });
    renderRomEditor();
    fireEvent.click(await findTreeTable(TRIM_B));
    const grid = screen.getByRole("grid", { name: `${TRIM_B} cells` });
    // The grid shows the value bits; the flag is in the raw count on hover.
    expect(cell(grid, 5, 10)).toHaveTextContent("47");
    expect(cell(grid, 5, 10)).toHaveAttribute("title", `raw ${0x80 | 47}`);
    expect(screen.getByText(/The ECM reads the low 7 bits of each cell/)).toBeVisible();
    fireEvent.mouseDown(cell(grid, 5, 10));
    fireEvent.mouseUp(window);
    const operation = screen.getByRole("form", { name: "Operation" });
    fireEvent.change(within(operation).getByLabelText("Operation"), {
      target: { value: "add_raw" },
    });
    type(within(operation).getByLabelText("Raw counts to add"), "-3");
    await click(within(operation).getByRole("button", { name: "Apply to selection" }));
    await showRomView("Edits & Preview");
    const pending = screen.getByRole("list", { name: "Pending edits" });
    expect(pending).toHaveTextContent("Ignition timing trim B: add raw -3 on 1 cell");
    type(screen.getByLabelText("Label for this edit set"), "trim B -3");
    await showRomView("Simulate");
    const form = screen.getByRole("form", { name: "Simulate form" });
    state(form);
    await click(runButton(form));
    await screen.findByRole("region", { name: "Simulation result" });
    expect(sentRequest()).toMatchObject({
      edits: [{ table: "timing_trim_b", slot: 1, cells: [[4, 9]], add_raw: -3 }],
      high_det: "set",
      knock_control: "fallback",
    });
    // The result: the change on the trims' own grid, at row 5 (2,000 rpm), column 10 (X 80).
    const timing = screen.getByRole("region", { name: "Simulation result" });
    const section = within(timing).getByRole("region", { name: "Ignition timing" });
    expect(section).toHaveTextContent("HI DET latch set (warm timing reads trim B)");
    const changes = within(section).getByRole("table", {
      name: "Ignition timing change per trim cell",
    });
    expect(within(changes).getByRole("cell", { name: /^Row 5, column 10: -3/ })).toHaveAttribute(
      "data-timing-mark",
      "changed",
    );
    expect(within(changes).getByRole("rowheader", { name: "2,000" })).toBeVisible();
    expect(within(changes).getByRole("columnheader", { name: "80" })).toBeVisible();
  });

  it("says a trim edit is read nowhere under the stated latch, not that it changed nothing", async () => {
    route({ simulate: { ...buildSimResult(), ignition_timing: buildTimingBlock("trim_a") } });
    renderRomEditor();
    fireEvent.click(await findTreeTable("Ignition timing trim A (Map 1)"));
    const grid = screen.getByRole("grid", { name: "Ignition timing trim A (Map 1) cells" });
    fireEvent.mouseDown(cell(grid, 4, 4));
    fireEvent.mouseUp(window);
    const operation = screen.getByRole("form", { name: "Operation" });
    fireEvent.change(within(operation).getByLabelText("Operation"), {
      target: { value: "add_raw" },
    });
    type(within(operation).getByLabelText("Raw counts to add"), "-3");
    await click(within(operation).getByRole("button", { name: "Apply to selection" }));
    type(screen.getByLabelText("Label for this edit set"), "trim A -3");
    await showRomView("Simulate");
    const form = screen.getByRole("form", { name: "Simulate form" });
    state(form);
    await click(runButton(form));
    const section = await screen.findByRole("region", { name: "Ignition timing" });
    expect(within(section).getAllByText("NR")).toHaveLength(2 + 1); // two cells, one legend swatch
    expect(section).toHaveTextContent("trim_a_not_read:high_det_latch_set");
    expect(section).toHaveTextContent("the HI DET latch is set, so the ECM reads trim B warm");
  });
});

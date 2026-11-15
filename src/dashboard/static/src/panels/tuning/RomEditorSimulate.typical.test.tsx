import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { HttpError, NetworkError } from "../../api/core";
import { SIM_STORAGE_KEY } from "../../lib/romSim";
import {
  TYPICAL,
  route,
  runButton,
  sentRequest,
  setUpSimulateTests,
  state,
  stateTiming,
  withPendingEdit,
  FUEL_TARGETS,
  wotNote,
  WOT_WARNING,
  field,
  presetState,
} from "../../test/romSimulateKit";
import { callsTo, click, renderRomEditor, type } from "../../test/controlRoutes";
import { chooseRecent, showRomView } from "../../test/romEditorViews";
import { documentedSimFields } from "../../test/romSimDoc";
import { SHIPPED_TYPICAL, leftEmptyOf, typicalWith } from "../../test/romTypicalFixtures";

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

// -- W09.11: Fill typical values -------------------------------------------------------------

const FILL = "Fill typical values";
const typicalButton = (form: HTMLElement) => within(form).getByRole("button", { name: FILL });
const typicalStatus = (form: HTMLElement) =>
  within(form).getByRole("status", { name: "Typical values state" });
const typicalCalls = () => callsTo(TYPICAL);
const typicalTags = (form: HTMLElement) =>
  Array.from(form.querySelectorAll<HTMLElement>("[data-typical]"));
const sourceOf = (name: string) => SHIPPED_TYPICAL.values[name]?.source ?? "";
const valueOf = (name: string) => String(SHIPPED_TYPICAL.values[name]?.value);

/** The form's labelled number fields and the request field each holds. */
const FILLED_FIELDS: [string, string][] = [
  ["Baro (kPa)", "baro_kpa"],
  ["IAT (°C)", "iat_c"],
  ["Coolant (°C)", "coolant_c"],
  ["Battery (V)", "battery_v"],
  ["Throttle (%)", "throttle_pct"],
  ["Pedal flag (0-255)", "pedal_flag"],
  ["STFT (%)", "stft_pct"],
  ["LTFT (%)", "ltft_pct"],
  ["EVAP purge area (counts)", "evap_purge_area"],
  ["Throttle closed learned (counts)", "throttle_closed_learned"],
  ["Idle air learned (counts)", "idle_air_learned"],
];
/** The two the Xterra's file declares as placeholders (kind `placeholder`). */
const PLACEHOLDER_FIELDS = ["Throttle closed learned (counts)", "Idle air learned (counts)"];
const tagText = (name: string) =>
  `${SHIPPED_TYPICAL.values[name]?.kind}: ${SHIPPED_TYPICAL.values[name]?.source}`;

async function pressFill(form: HTMLElement) {
  await click(typicalButton(form));
  await waitFor(() => expect(typicalStatus(form)).not.toHaveTextContent(/^$/));
}

describe("Fill typical values", () => {
  it("fetches nothing until it is pressed, and is on the page with what it does", async () => {
    route({ typical: SHIPPED_TYPICAL });
    const form = await withPendingEdit();
    expect(typicalCalls()).toHaveLength(0);
    expect(typicalButton(form)).toBeEnabled();
    expect(form).toHaveTextContent(/It is a click, not a default/);
    expect(typicalStatus(form)).toHaveTextContent(/^$/);
    expect(typicalTags(form)).toHaveLength(0);
  });

  it("fills exactly the declared fields, tags each with its kind and source, and calls the two it cannot measure placeholders", async () => {
    route({ typical: SHIPPED_TYPICAL });
    const form = await withPendingEdit();
    const edges = [field(form, "RPM edges (rpm)").value, field(form, "Load edges (% fill)").value];
    await pressFill(form);
    expect(typicalCalls()).toHaveLength(1);
    expect(typicalCalls()[0].method).toBe("GET");

    for (const [label, name] of FILLED_FIELDS) {
      expect(field(form, label).value, label).toBe(valueOf(name));
    }
    // The rail is a stated number here, so its choice and its value are both filled.
    expect(within(form).getByLabelText<HTMLSelectElement>("Rail pressure").value).toBe("stated");
    expect(field(form, "Rail pressure (kPa)").value).toBe(valueOf("rail_kpa"));

    // Each filled field carries its kind and source, visibly: twelve values, thirteen
    // fields (the rail is two). Ten are `typical`, two are `placeholder`.
    const tags = typicalTags(form).map((tag) => tag.textContent);
    expect(tags).toHaveLength(13);
    expect(new Set(tags)).toEqual(
      new Set(Object.keys(SHIPPED_TYPICAL.values).map((name) => tagText(name))),
    );
    expect(tags.filter((tag) => tag?.startsWith("typical: "))).toHaveLength(11);
    expect(tags.filter((tag) => tag?.startsWith("placeholder: "))).toHaveLength(2);
    expect(form.querySelectorAll('[data-typical-kind="placeholder"]')).toHaveLength(2);
    // A placeholder is never tagged `typical`, whatever sits in the field.
    for (const label of PLACEHOLDER_FIELDS) {
      const described = document.getElementById(
        field(form, label).getAttribute("aria-describedby") ?? "",
      );
      expect(described).toHaveTextContent(/^placeholder: PLACEHOLDER, not a measurement/);
      expect(described?.getAttribute("data-typical-kind")).toBe("placeholder");
    }
    const baro = field(form, "Baro (kPa)");
    expect(document.getElementById(baro.getAttribute("aria-describedby") ?? "")).toHaveTextContent(
      tagText("baro_kpa"),
    );

    // The report: what it filled, what it called a placeholder, nothing left empty.
    const report = typicalStatus(form);
    expect(report).toHaveTextContent(
      /Filled 12 values \(10 typical, 2 placeholders\) from nissan-xterra-2008 \(sim_typical_values\.yaml, sha256 [0-9a-f]{8}\)/,
    );
    const filled = within(report).getByRole("list", { name: "Filled typical values" });
    expect(within(filled).getAllByRole("listitem")).toHaveLength(12);
    expect(filled).toHaveTextContent(/Throttle closed learned \(counts\): 0 \(placeholder\)/);
    expect(filled).toHaveTextContent(/Idle air learned \(counts\): 0 \(placeholder\)/);
    expect(report).toHaveTextContent(
      "2 of them are placeholders, not measurements: Throttle closed learned (counts), Idle air learned (counts).",
    );
    expect(within(report).queryByRole("list", { name: "Left empty" })).toBeNull();
    expect(report).not.toHaveTextContent(/Left empty for you to state/);
    expect(report).toHaveTextContent(
      /Nothing else was touched: the grid, the HI DET latch, knock control, the candidate events/,
    );

    // Nothing the click does not own moved: choices, edges, draws, seed and the label.
    for (const radio of within(form).getAllByRole("radio")) expect(radio).not.toBeChecked();
    expect([
      field(form, "RPM edges (rpm)").value,
      field(form, "Load edges (% fill)").value,
    ]).toEqual(edges);
    expect(field(form, "Draws").value).toBe("");
    expect(field(form, "Seed").value).toBe("");
    expect(screen.getByLabelText<HTMLInputElement>("Label for this edit set").value).toBe(
      "idle richer",
    );
    expect(presetState(form)).toHaveTextContent("No preset pressed");
  });

  it("a vehicle that leaves fields empty: names them with the file's reason, leaves them blank, and Run waits for them", async () => {
    route({ typical: leftEmptyOf("throttle_closed_learned", "idle_air_learned") });
    const form = await withPendingEdit();
    await pressFill(form);
    for (const label of PLACEHOLDER_FIELDS) expect(field(form, label).value, label).toBe("");
    expect(typicalTags(form)).toHaveLength(11);
    const report = typicalStatus(form);
    expect(report).toHaveTextContent(/Filled 10 typical values from nissan-xterra-2008/);
    expect(report).toHaveTextContent(/Left empty for you to state \(2\)/);
    const left = within(report).getByRole("list", { name: "Left empty" });
    expect(left).toHaveTextContent(/Throttle closed learned \(counts\): no value is declared/);
    expect(left).toHaveTextContent(/Idle air learned \(counts\): no value is declared/);
    expect(report).not.toHaveTextContent(/placeholder/);
    expect(within(form).getByRole("status", { name: "What blocks Run" })).toHaveTextContent(
      "Throttle closed learned (counts), Idle air learned (counts)",
    );
  });

  it("leaves a filled form with the grid and events unchosen blocked, and names them", async () => {
    route({ typical: SHIPPED_TYPICAL });
    const form = await withPendingEdit();
    await pressFill(form);
    // Everything the click did not fill is still missing, and Run says so.
    const blocks = within(form).getByRole("status", { name: "What blocks Run" });
    for (const name of ["Grid", "Candidate events", "Draws", "Seed"]) {
      expect(blocks).toHaveTextContent(name);
    }
    // Every stated field, the two placeholders included, is filled: Run needs none of them.
    for (const name of ["Baro", "Throttle closed learned", "Idle air learned", "Pedal flag"]) {
      expect(blocks).not.toHaveTextContent(name);
    }
    expect(runButton(form)).toBeDisabled();
    // State what is left, and Run still waits for the two experiment choices.
    // The suggested load edges run past the model's 100 %, which blocks Run by itself.
    type(field(form, "RPM edges (rpm)"), "200, 600, 1000, 1400");
    type(field(form, "Load edges (% fill)"), "3.5, 10.7, 17.8, 25");
    type(field(form, "Draws"), "200");
    type(field(form, "Seed"), "20261015");
    expect(within(form).getByRole("status", { name: "What blocks Run" })).toHaveTextContent(
      "Run needs: Grid, HI DET latch, Knock control, Candidate events.",
    );
    expect(runButton(form)).toBeDisabled();
    fireEvent.click(within(form).getByRole("radio", { name: /Fuel Comp axes/ }));
    fireEvent.click(within(form).getByRole("radio", { name: /Same learned state on both/ }));
    stateTiming(form);
    expect(runButton(form)).toBeEnabled();
  });

  it("sends exactly the 23 documented fields after a fill, the filled values as shown", async () => {
    route({ typical: SHIPPED_TYPICAL });
    const form = await withPendingEdit();
    await pressFill(form);
    // The two placeholders went in with the click: Run needs only the edges, draws, seed
    // and the two experiment choices (the suggested load edges pass 100 %, so they are typed).
    type(field(form, "RPM edges (rpm)"), "200, 600, 1000, 1400");
    type(field(form, "Load edges (% fill)"), "3.5, 10.7, 17.8, 25");
    type(field(form, "Draws"), "200");
    type(field(form, "Seed"), "20261015");
    fireEvent.click(within(form).getByRole("radio", { name: /Fuel Comp axes/ }));
    fireEvent.click(within(form).getByRole("radio", { name: /Fresh flash resets trims/ }));
    stateTiming(form);
    await click(runButton(form));
    await screen.findByRole("region", { name: "Simulation result" });
    const request = sentRequest();
    expect(Object.keys(request).sort()).toEqual([...documentedSimFields()].sort());
    expect(request).toHaveProperty("rail_kpa", Number(valueOf("rail_kpa")));
    for (const [, name] of FILLED_FIELDS) {
      expect(request[name], name).toBe(Number(valueOf(name)));
    }
    expect(request).toMatchObject({
      grid: "fuel_comp",
      candidate_events: "ecu_reflash",
      draws: 200,
      seed: 20261015,
      throttle_closed_learned: 0,
      idle_air_learned: 0,
    });
  });

  it("overwrites only the fields it fills, lists what it replaced, and keeps every choice of yours", async () => {
    // The choice (stated here, not discovered): a click replaces the fields the
    // vehicle declares (placeholders included), and the report names each value of
    // yours it replaced; it never touches a field the vehicle left empty, nor the
    // grid, events, edges, draws or seed.
    route({ typical: SHIPPED_TYPICAL });
    const form = await withPendingEdit();
    state(form);
    type(field(form, "Baro (kPa)"), "80");
    type(field(form, "Throttle closed learned (counts)"), "150");
    const before = {
      edges: [field(form, "RPM edges (rpm)").value, field(form, "Load edges (% fill)").value],
      draws: field(form, "Draws").value,
      seed: field(form, "Seed").value,
    };
    await pressFill(form);
    expect(field(form, "Baro (kPa)").value).toBe(valueOf("baro_kpa"));
    expect(field(form, "LTFT (%)").value).toBe(valueOf("ltft_pct"));
    expect(within(form).getByLabelText<HTMLSelectElement>("Rail pressure").value).toBe("stated");
    // A placeholder replaces what you typed, and says so below.
    expect(field(form, "Throttle closed learned (counts)").value).toBe("0");
    expect(field(form, "Idle air learned (counts)").value).toBe("0");
    // The experiment and the run are yours.
    expect(within(form).getByRole("radio", { name: /Fuel Comp axes/ })).toBeChecked();
    expect(within(form).getByRole("radio", { name: /Fresh flash resets trims/ })).toBeChecked();
    expect([
      field(form, "RPM edges (rpm)").value,
      field(form, "Load edges (% fill)").value,
    ]).toEqual(before.edges);
    expect(field(form, "Draws").value).toBe(before.draws);
    expect(field(form, "Seed").value).toBe(before.seed);
    // What it replaced is listed: baro (80), LTFT (0), the rail choice and the 150 in a
    // placeholder's field; a value that was already the declared number (IAT 25, coolant
    // 88, idle air 0 ...) is not.
    const filled = within(typicalStatus(form)).getByRole("list", { name: "Filled typical values" });
    const replaced = within(filled)
      .getAllByRole("listitem")
      .filter((item) => /replaced your/.test(item.textContent ?? ""))
      .map((item) => item.textContent);
    expect(replaced).toHaveLength(4);
    expect(replaced.join(" | ")).toMatch(
      /Throttle closed learned \(counts\): 0 \(placeholder\) \(replaced your 150\)/,
    );
    expect(replaced.join(" | ")).toMatch(/Baro \(kPa\): 81 \(replaced your 80\)/);
    expect(replaced.join(" | ")).toMatch(/LTFT \(%\): 2\.34 \(replaced your 0\)/);
    expect(replaced.join(" | ")).toMatch(
      /Rail pressure \(kPa\): 350 \(replaced your the plant's working assumption\)/,
    );
  });

  it("keeps your value in a field the vehicle left empty, and in the draws and the seed", async () => {
    route({ typical: leftEmptyOf("throttle_closed_learned", "idle_air_learned") });
    const form = await withPendingEdit();
    state(form);
    type(field(form, "Throttle closed learned (counts)"), "150");
    type(field(form, "Idle air learned (counts)"), "40");
    await pressFill(form);
    expect(field(form, "Throttle closed learned (counts)").value).toBe("150");
    expect(field(form, "Idle air learned (counts)").value).toBe("40");
    expect(field(form, "Draws").value).toBe("200");
    expect(field(form, "Seed").value).toBe("20261015");
    expect(typicalTags(form)).toHaveLength(11);
    expect(typicalStatus(form)).toHaveTextContent(/Left empty for you to state \(2\)/);
  });

  it("loses a field's tag when the operator edits that field, and only that field's", async () => {
    route({ typical: SHIPPED_TYPICAL });
    const form = await withPendingEdit();
    await pressFill(form);
    expect(typicalTags(form)).toHaveLength(13);
    type(field(form, "Baro (kPa)"), "78");
    expect(typicalTags(form)).toHaveLength(12);
    expect(field(form, "Baro (kPa)").value).toBe("78");
    expect(field(form, "Baro (kPa)")).not.toHaveAttribute("aria-describedby");
    expect(typicalTags(form).map((tag) => tag.textContent)).not.toContain(
      `typical: ${sourceOf("baro_kpa")}`,
    );
    // The rail's choice and its value are two fields with a tag each; choosing the
    // working assumption clears the choice's and takes the number's field off the page.
    fireEvent.change(within(form).getByLabelText("Rail pressure"), {
      target: { value: "working_assumption" },
    });
    expect(typicalTags(form)).toHaveLength(10);
    type(field(form, "LTFT (%)"), "1");
    expect(typicalTags(form)).toHaveLength(9);
    // Stating a placeholder's field yourself takes its placeholder tag off too.
    type(field(form, "Idle air learned (counts)"), "37");
    expect(typicalTags(form)).toHaveLength(8);
    expect(form.querySelectorAll('[data-typical-kind="placeholder"]')).toHaveLength(1);
    expect(field(form, "IAT (°C)")).toHaveAttribute("aria-describedby");
  });

  it("takes a remembered field over: the remembered mark goes, the typical tag comes", async () => {
    window.localStorage.setItem(
      SIM_STORAGE_KEY,
      JSON.stringify({ version: 1, savedAt: "2026-10-10T00:00:00Z", values: { baro: "80" } }),
    );
    route({ typical: SHIPPED_TYPICAL });
    const form = await withPendingEdit();
    expect(form.querySelectorAll("[data-remembered]")).toHaveLength(1);
    expect(field(form, "Baro (kPa)").value).toBe("80");
    await pressFill(form);
    expect(form.querySelectorAll("[data-remembered]")).toHaveLength(0);
    expect(field(form, "Baro (kPa)").value).toBe(valueOf("baro_kpa"));
    expect(typicalTags(form)).toHaveLength(13);
    expect(form).not.toHaveTextContent(/remembered from your last run and are marked/);
  });

  it("does not hide the WOT-column trap: the flag is shown, explained, and the warning still fires", async () => {
    route({ typical: SHIPPED_TYPICAL });
    const form = await withPendingEdit("wot richer", {
      table: FUEL_TARGETS,
      from: [3, 8],
      to: [4, 8],
    });
    expect(wotNote(form)).toBeNull();
    await pressFill(form);
    // The declared flag (0, W09.6's grid environment) is in the field, tagged.
    expect(field(form, "Pedal flag (0-255)").value).toBe("0");
    const tag = document.getElementById(
      field(form, "Pedal flag (0-255)").getAttribute("aria-describedby") ?? "",
    );
    expect(tag).toHaveTextContent(tagText("pedal_flag"));
    expect(tag).toHaveTextContent(/^typical: /);
    // The fill message says what the flag does, in one line.
    expect(typicalStatus(form)).toHaveTextContent(
      "Pedal flag 0: bit 0 is clear, so no simulated point reads the last Target AFR column (the WOT column) and an edit to that column shows no change.",
    );
    // And the existing W09.9 warning is untouched: a WOT-column edit with the flag at 0.
    expect(wotNote(form)).toHaveTextContent(WOT_WARNING);
  });

  it("explains a pedal flag with bit 0 set instead, and then the warning is quiet", async () => {
    route({
      typical: typicalWith({
        values: {
          ...SHIPPED_TYPICAL.values,
          pedal_flag: { value: 1, kind: "typical", source: "a WOT case" },
        },
      }),
    });
    const form = await withPendingEdit("wot richer", {
      table: FUEL_TARGETS,
      from: [3, 8],
      to: [4, 8],
    });
    await pressFill(form);
    expect(field(form, "Pedal flag (0-255)").value).toBe("1");
    expect(typicalStatus(form)).toHaveTextContent(/Pedal flag 1: bit 0 is set/);
    expect(wotNote(form)).toBeNull();
  });

  it("names a field and a value it cannot fill, and fills nothing it was not given", async () => {
    route({
      typical: typicalWith({
        values: {
          ...SHIPPED_TYPICAL.values,
          draws: { value: 5, kind: "typical", source: "a bad file" },
          iat_c: { value: 25, kind: "typical", source: "" },
        },
      }),
    });
    const form = await withPendingEdit();
    await pressFill(form);
    expect(field(form, "Draws").value).toBe("");
    expect(field(form, "IAT (°C)").value).toBe("");
    const skipped = within(typicalStatus(form)).getByRole("list", { name: "Not filled" });
    expect(skipped).toHaveTextContent(/draws: this dashboard has no form field for it/);
    expect(skipped).toHaveTextContent(/IAT \(°C\): the daemon sent no source for it/);
    expect(typicalStatus(form)).toHaveTextContent(/Filled 11 values \(9 typical, 2 placeholders\)/);
  });

  it("is off when Simulate is, with the form's own reason", async () => {
    route({ typical: SHIPPED_TYPICAL });
    renderRomEditor();
    await screen.findByRole("grid");
    await showRomView("Simulate");
    chooseRecent(/Edit from k106\.bin/);
    await screen.findByRole("note");
    const form = screen.getByRole("form", { name: "Simulate form" });
    expect(typicalButton(form)).toBeDisabled();
    expect(typicalCalls()).toHaveLength(0);
  });
});

describe("Fill typical values: when the values cannot be had, the form is left as it was", () => {
  const http = (status: number, body: unknown) => new HttpError(status, JSON.stringify(body));
  const CASES: [string, Error, RegExp, string | null][] = [
    [
      "a vehicle that declares none",
      http(409, {
        detail: {
          error: "typical_values_undeclared",
          message:
            "vehicle-x has no sim_typical_values.yaml: this vehicle declares no typical values",
        },
      }),
      /This vehicle declares no typical values \(typical_values_undeclared\)\./,
      "typical_values_undeclared",
    ],
    [
      "a broken file",
      http(409, {
        detail: {
          error: "typical_values_invalid",
          message: "sim_typical_values.yaml: values.baro_kpa.source: Field required",
        },
      }),
      /This vehicle's typical values file is invalid \(typical_values_invalid\)\./,
      "typical_values_invalid",
    ],
    ["a refused token", http(401, { detail: "Unauthorized" }), /Not authorized/, null],
    [
      "an older daemon",
      http(404, { detail: "Not Found" }),
      /This daemon does not serve typical values/,
      null,
    ],
    ["a server error", new HttpError(500, "boom"), /Server error 500/, null],
  ];

  it.each(CASES)("%s: names the reason and changes nothing", async (_name, error, title) => {
    route({ typical: error });
    const form = await withPendingEdit();
    state(form, ["Baro (kPa)", "Throttle closed learned (counts)"]);
    type(field(form, "Baro (kPa)"), "80");
    const snapshot = () =>
      JSON.stringify(
        Array.from(
          form.querySelectorAll<HTMLInputElement | HTMLSelectElement>("input, select"),
        ).map((element) =>
          element instanceof HTMLInputElement && element.type === "radio"
            ? element.checked
            : element.value,
        ),
      );
    const before = snapshot();
    await pressFill(form);
    expect(typicalStatus(form)).toHaveTextContent(title);
    expect(typicalStatus(form)).toHaveTextContent("The form was not changed.");
    expect(snapshot()).toBe(before);
    expect(typicalTags(form)).toHaveLength(0);
    // The button stays, enabled: it explains rather than vanishing.
    expect(typicalButton(form)).toBeEnabled();
  });

  it("names the reason of a vehicle with no typical values in the report, with the daemon's words", async () => {
    route({ typical: CASES[0][1] });
    const form = await withPendingEdit();
    await pressFill(form);
    expect(typicalStatus(form)).toHaveTextContent(/this vehicle declares no typical values/i);
    expect(typicalStatus(form)).toHaveTextContent("vehicle-x has no sim_typical_values.yaml");
  });

  it("an unreachable daemon is said to be unreachable, not a bug", async () => {
    route({ typical: new NetworkError("/control/rom/editor/typical-values") });
    const form = await withPendingEdit();
    await pressFill(form);
    expect(typicalStatus(form)).toHaveTextContent(/Can't reach the daemon/);
    expect(typicalTags(form)).toHaveLength(0);
  });

  it("retries after a failure, then uses what it fetched without asking again", async () => {
    let calls = 0;
    route({
      typical: () => {
        calls += 1;
        if (calls === 1) throw http(503, { detail: "not wired" });
        return SHIPPED_TYPICAL;
      },
    });
    const form = await withPendingEdit();
    await pressFill(form);
    expect(typicalTags(form)).toHaveLength(0);
    await click(typicalButton(form));
    await waitFor(() => expect(typicalTags(form)).toHaveLength(13));
    expect(typicalCalls()).toHaveLength(2);
    // Edit a filled field, press again: the cached values go back in, with no new request.
    type(field(form, "Baro (kPa)"), "78");
    await click(typicalButton(form));
    expect(field(form, "Baro (kPa)").value).toBe(valueOf("baro_kpa"));
    expect(typicalCalls()).toHaveLength(2);
    expect(typicalStatus(form)).toHaveTextContent(/Baro \(kPa\): 81 \(replaced your 78\)/);
  });
});

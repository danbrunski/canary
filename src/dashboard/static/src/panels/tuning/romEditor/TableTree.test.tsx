/**
 * The ROM Editor's table picker (W10.10-W10.11, W10.14-W10.15): every
 * non-axis table of every slot, grouped as `rom_map.yaml` files them, a
 * slotted table's copies under one family, with Editable / View only / All, a
 * text filter, collapsible groups and the marks for editable, pending and an
 * unconfirmed decode; below 900px the same choice as a native select.
 */
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  BASE_TIMING_TABLE,
  TABLES,
  TABLES_WITH_CURVES,
  fuelCompTable,
} from "../../../test/romLibraryFixtures";
import type { PendingEdit } from "../../../types/romEditor";
import type { RomTableValues } from "../../../types/romLibrary";
import { TABLE_KINDS_STORAGE_KEY, TableTree } from "./TableTree";

const [FUEL_TARGETS, K] = TABLES.tables;
function fuelTargets(slot: number): RomTableValues {
  return slot === 1
    ? FUEL_TARGETS
    : { ...TABLES.tables[2], label: `Fuel Targets (Map ${slot})`, slot };
}
const FUEL_COMP = fuelCompTable(1);
function byId(id: string): RomTableValues {
  const found = TABLES_WITH_CURVES.tables.find((table) => table.id === id);
  if (!found) throw new Error(`no fixture table ${id}`);
  return found;
}
const CURVE = byId("throttle_area_curve");
const SCALAR = byId("idle_air_cap");
const TRIM_A: RomTableValues = {
  ...BASE_TIMING_TABLE,
  id: "timing_trim_a",
  name: "Timing Main (trim A)",
  label: "Timing Main (trim A) (Map 1)",
  kind: "timing_trim_a",
  confidence: "verified",
};
/** A daemon older than W10.7 sends no group. */
const NO_GROUP: RomTableValues = {
  ...BASE_TIMING_TABLE,
  id: "mystery",
  name: "Mystery table",
  label: "Mystery table (Map 1)",
  group: undefined,
};
const LONG: RomTableValues = {
  ...BASE_TIMING_TABLE,
  id: "timing_retard_26d9_map",
  name: "Timing retard 0xFFFF26D0 rpm by BFS by coolant (Map 1 slot copy)",
  short_name: "Timing retard 0xFFFF26D0 rpm/BFS/coolant",
  label: "Timing retard 0xFFFF26D0 rpm by BFS by coolant (Map 1 slot copy) (Map 1)",
};

/** Map 1 and single-copy tables, as one slot listed them before W10.14. */
const MAP_1 = [FUEL_TARGETS, NO_GROUP, TRIM_A, FUEL_COMP, CURVE, BASE_TIMING_TABLE, SCALAR];
/** The daemon's order: Map 1's copies first, Maps 2-3 after. */
const SLOTTED = [
  FUEL_TARGETS,
  FUEL_COMP,
  BASE_TIMING_TABLE,
  fuelTargets(2),
  fuelCompTable(2),
  fuelTargets(3),
  fuelCompTable(3),
];

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

function pendingOn(table: RomTableValues, count: number): PendingEdit[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `${table.id}-${table.slot}-${i}`,
    summary: "scale",
    tableName: table.name,
    chain: null,
    edit: {
      table: table.id ?? "",
      slot: table.slot ?? 1,
      cells: [[i, 0]],
      scale: 1.02,
      rounding: "nearest",
    },
  }));
}

function renderTree(overrides: Partial<Parameters<typeof TableTree>[0]> = {}): {
  onChoose: ReturnType<typeof vi.fn>;
} {
  const onChoose = vi.fn();
  render(
    <TableTree
      tables={MAP_1}
      chosen={FUEL_TARGETS}
      pending={[]}
      narrow={false}
      onChoose={onChoose}
      {...overrides}
    />,
  );
  return { onChoose };
}

const tree = () => screen.getByRole("navigation", { name: "Tables" });
const kinds = () => screen.getByRole("group", { name: "Show tables" });
const showKinds = (label: string) =>
  fireEvent.click(within(kinds()).getByRole("button", { name: label }));
const family = (name: RegExp | string) => within(tree()).getByRole("button", { name });
/** Group headings, family rows and table names, top to bottom, as assistive
 *  tech reads them (the marks are aria-hidden); the Show tables choice is
 *  left out. */
const read = () =>
  within(tree())
    .getAllByRole("button")
    .filter((button) => !kinds().contains(button))
    .map(
      (button) =>
        button.getAttribute("aria-label") ??
        [...button.childNodes]
          .filter((node) => !(node instanceof Element && node.getAttribute("aria-hidden")))
          .map((node) => node.textContent)
          .join(""),
    );

describe("TableTree", () => {
  it("groups in the schema's order, the daemon's order within a group, and Other last", () => {
    renderTree();
    const headings = within(tree())
      .getAllByRole("button", { expanded: true })
      .map((button) => button.getAttribute("aria-label"));
    expect(headings).toEqual([
      "Fuel, 2 tables",
      "Ignition, 2 tables",
      "Airflow, 2 tables",
      "Other, 1 table",
    ]);
    const names = within(tree())
      .getAllByRole("listitem")
      .map((item) => within(item).getByRole("button").title.split(" · ")[0]);
    expect(names).toEqual([
      "Fuel Targets (Map 1)",
      "Fuel Compensation (Map 1)",
      "Timing Main (trim A) (Map 1)",
      "Base timing rate map (Map 1)",
      "Throttle effective area",
      "Idle air learning cap",
      "Mystery table (Map 1)",
    ]);
  });

  it("names each entry by its short name, else its name, and keeps the full label as the title", () => {
    renderTree({ tables: [FUEL_TARGETS, LONG] });
    const long = within(tree()).getByRole("button", {
      name: "Timing retard 0xFFFF26D0 rpm/BFS/coolant",
    });
    expect(long.title).toBe(
      "Timing retard 0xFFFF26D0 rpm by BFS by coolant (Map 1 slot copy) (Map 1) · decode inferred",
    );
    expect(within(tree()).getByRole("button", { name: "Fuel Targets" })).toBeVisible();
  });

  it("marks the chosen table aria-current and hands a click to onChoose", () => {
    const { onChoose } = renderTree();
    expect(within(tree()).getByRole("button", { name: "Fuel Targets" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(within(tree()).getByRole("button", { name: "Fuel Compensation" })).not.toHaveAttribute(
      "aria-current",
    );
    fireEvent.click(within(tree()).getByRole("button", { name: "Fuel Compensation" }));
    expect(onChoose).toHaveBeenCalledWith(FUEL_COMP);
  });

  it("says in the title what the marks show: editable, pending edits, an unconfirmed decode", () => {
    renderTree({
      tables: [FUEL_TARGETS, FUEL_COMP, BASE_TIMING_TABLE],
      pending: [...pendingOn(FUEL_COMP, 2), ...pendingOn(FUEL_TARGETS, 1)],
    });
    const button = (name: string) => within(tree()).getByRole("button", { name });
    expect(button("Fuel Targets").title).toBe("Fuel Targets (Map 1) · editable · 1 pending edit");
    expect(button("Fuel Compensation").title).toBe(
      "Fuel Compensation (Map 1) · editable · 2 pending edits",
    );
    expect(button("Base timing rate map").title).toBe(
      "Base timing rate map (Map 1) · decode inferred",
    );
    // The marks themselves are hidden from the name: it stays the label.
    expect(button("Fuel Compensation")).toHaveTextContent("Fuel Compensation✎2");
  });

  it("filters on the short name and the name, any case, hiding empty groups", () => {
    renderTree({ tables: [...MAP_1, LONG] });
    const filter = screen.getByRole("searchbox", { name: "Filter tables" });
    fireEvent.change(filter, { target: { value: "TIMING" } });
    expect(read()).toEqual([
      "Ignition, 3 tables",
      "Timing Main (trim A)",
      "Base timing rate map",
      "Timing retard 0xFFFF26D0 rpm/BFS/coolant",
    ]);
    // The name is searched too, where the short name differs.
    fireEvent.change(filter, { target: { value: "slot copy" } });
    expect(read()).toEqual(["Ignition, 1 table", "Timing retard 0xFFFF26D0 rpm/BFS/coolant"]);
    fireEvent.change(filter, { target: { value: "boost" } });
    expect(read()).toEqual([]);
    expect(screen.getByRole("status")).toHaveTextContent("No table matches “boost”.");
  });

  it("collapses a group from its heading, and a filter shows a collapsed group's matches", () => {
    renderTree();
    const fuel = within(tree()).getByRole("button", { name: "Fuel, 2 tables" });
    fireEvent.click(fuel);
    expect(fuel).toHaveAttribute("aria-expanded", "false");
    expect(within(tree()).queryByRole("button", { name: "Fuel Targets" })).not.toBeInTheDocument();
    expect(within(tree()).getByRole("button", { name: "Timing Main (trim A)" })).toBeVisible();
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter tables" }), {
      target: { value: "comp" },
    });
    expect(within(tree()).getByRole("button", { name: "Fuel Compensation" })).toBeVisible();
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter tables" }), {
      target: { value: "" },
    });
    expect(within(tree()).queryByRole("button", { name: "Fuel Targets" })).not.toBeInTheDocument();
    fireEvent.click(fuel);
    expect(within(tree()).getByRole("button", { name: "Fuel Targets" })).toBeVisible();
  });

  it("has no map slot choice: the slot is the chosen table's own", () => {
    renderTree({ tables: SLOTTED });
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Map slot")).not.toBeInTheDocument();
  });
});

describe("TableTree: map slot families (W10.14)", () => {
  it("folds a slotted table's copies into one family where its first copy stands, in slot order", () => {
    renderTree({ tables: SLOTTED });
    expect(read()).toEqual([
      "Fuel, 2 tables",
      "Fuel Targets, 3 maps",
      "Map 1",
      "Map 2",
      "Map 3",
      "Fuel Compensation, 3 maps",
      "Ignition, 1 table",
      "Base timing rate map",
    ]);
    const group = within(tree()).getByRole("group", { name: "Fuel Targets" });
    expect(
      within(group)
        .getAllByRole("button")
        .map((button) => button.title.split(" · ")[0]),
    ).toEqual(["Fuel Targets (Map 1)", "Fuel Targets (Map 2)", "Fuel Targets (Map 3)"]);
  });

  it("opens the chosen table's family and leaves the rest closed, each opening from its row", () => {
    const { onChoose } = renderTree({ tables: SLOTTED, chosen: fuelTargets(2) });
    expect(family("Fuel Targets, 3 maps")).toHaveAttribute("aria-expanded", "true");
    expect(within(tree()).getByRole("button", { name: "Map 2" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    const comp = family("Fuel Compensation, 3 maps");
    expect(comp).toHaveAttribute("aria-expanded", "false");
    expect(within(tree()).queryByRole("group", { name: "Fuel Compensation" })).toBeNull();
    fireEvent.click(comp);
    expect(comp).toHaveAttribute("aria-expanded", "true");
    const copies = within(tree()).getByRole("group", { name: "Fuel Compensation" });
    fireEvent.click(within(copies).getByRole("button", { name: "Map 3" }));
    expect(onChoose).toHaveBeenCalledWith(fuelCompTable(3));
    // A row closes as it opened.
    fireEvent.click(comp);
    expect(within(tree()).queryByRole("group", { name: "Fuel Compensation" })).toBeNull();
  });

  it("opens the chosen table's family when the choice comes from elsewhere", () => {
    const { rerender } = render(
      <TableTree
        tables={SLOTTED}
        chosen={FUEL_TARGETS}
        pending={[]}
        narrow={false}
        onChoose={() => {}}
      />,
    );
    expect(family("Fuel Compensation, 3 maps")).toHaveAttribute("aria-expanded", "false");
    rerender(
      <TableTree
        tables={SLOTTED}
        chosen={fuelCompTable(2)}
        pending={[]}
        narrow={false}
        onChoose={() => {}}
      />,
    );
    expect(family("Fuel Compensation, 3 maps")).toHaveAttribute("aria-expanded", "true");
    // Fuel Targets keeps the state it had.
    expect(family("Fuel Targets, 3 maps")).toHaveAttribute("aria-expanded", "true");
  });

  it("marks a family editable when a copy is, with its copies' pending edits", () => {
    renderTree({
      tables: SLOTTED,
      chosen: BASE_TIMING_TABLE,
      pending: [...pendingOn(FUEL_COMP, 2), ...pendingOn(FUEL_TARGETS, 1)],
    });
    // Closed, the row still says what is inside: an editable copy, 2 edits.
    const comp = family("Fuel Compensation, 3 maps");
    expect(comp.title).toBe("Fuel Compensation · 3 maps");
    expect(comp).toHaveTextContent("Fuel Compensation✎2");
    expect(family("Fuel Targets, 3 maps")).toHaveTextContent("Fuel Targets✎1");
    fireEvent.click(comp);
    const copies = within(tree()).getByRole("group", { name: "Fuel Compensation" });
    expect(within(copies).getByRole("button", { name: "Map 1" }).title).toBe(
      "Fuel Compensation (Map 1) · editable · 2 pending edits",
    );
    expect(within(copies).getByRole("button", { name: "Map 2" }).title).toBe(
      "Fuel Compensation (Map 2) · decode inferred",
    );
  });

  it("opens every family a text filter matches, and lists a family left with one copy as that copy", () => {
    renderTree({ tables: SLOTTED, chosen: BASE_TIMING_TABLE });
    const filter = screen.getByRole("searchbox", { name: "Filter tables" });
    fireEvent.change(filter, { target: { value: "fuel" } });
    expect(read()).toEqual([
      "Fuel, 2 tables",
      "Fuel Targets, 3 maps",
      "Map 1",
      "Map 2",
      "Map 3",
      "Fuel Compensation, 3 maps",
      "Map 1",
      "Map 2",
      "Map 3",
    ]);
    // Editable keeps only Map 1's copies: one each, so no family.
    showKinds("Editable");
    expect(read()).toEqual(["Fuel, 2 tables", "Fuel Targets", "Fuel Compensation"]);
    expect(within(tree()).getByRole("button", { name: "Fuel Targets" }).title).toBe(
      "Fuel Targets (Map 1) · editable",
    );
  });

  it("lists an image without map slots as plain entries", () => {
    const stock = [FUEL_TARGETS, FUEL_COMP, BASE_TIMING_TABLE].map((table) => ({
      ...table,
      slot: null,
      label: table.name,
    }));
    renderTree({ tables: stock, chosen: stock[0] });
    expect(read()).toEqual([
      "Fuel, 2 tables",
      "Fuel Targets",
      "Fuel Compensation",
      "Ignition, 1 table",
      "Base timing rate map",
    ]);
    expect(within(tree()).queryAllByRole("group", { name: /Fuel/ })).toEqual([]);
  });
});

describe("TableTree: Editable / View only / All (W10.15)", () => {
  it("shows All by default, and Editable or View only on request, with the text filter", () => {
    renderTree({ tables: [...MAP_1, K] });
    expect(within(kinds()).getByRole("button", { name: "All" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    showKinds("Editable");
    expect(read()).toEqual([
      "Fuel, 3 tables",
      "Fuel Targets",
      "Fuel Compensation",
      "K (injector constant)",
      "Ignition, 1 table",
      "Timing Main (trim A)",
    ]);
    showKinds("View only");
    expect(read()).toEqual([
      "Ignition, 1 table",
      "Base timing rate map",
      "Airflow, 2 tables",
      "Throttle effective area",
      "Idle air learning cap",
      "Other, 1 table",
      "Mystery table",
    ]);
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter tables" }), {
      target: { value: "timing" },
    });
    expect(read()).toEqual(["Ignition, 1 table", "Base timing rate map"]);
    showKinds("All");
    expect(read()).toEqual(["Ignition, 2 tables", "Timing Main (trim A)", "Base timing rate map"]);
  });

  it("says why the tree is empty: the text, else the kind", () => {
    renderTree({ tables: [BASE_TIMING_TABLE, CURVE], chosen: BASE_TIMING_TABLE });
    showKinds("Editable");
    expect(read()).toEqual([]);
    expect(screen.getByRole("status")).toHaveTextContent("No editable table in this image.");
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter tables" }), {
      target: { value: "boost" },
    });
    expect(screen.getByRole("status")).toHaveTextContent("No table matches “boost”.");
    cleanup();
    renderTree({ tables: [FUEL_TARGETS, FUEL_COMP] });
    showKinds("View only");
    expect(screen.getByRole("status")).toHaveTextContent("No view-only table in this image.");
  });

  it("keeps the chosen table chosen when a kind hides it", () => {
    const { onChoose } = renderTree();
    showKinds("View only");
    expect(within(tree()).queryByRole("button", { name: "Fuel Targets" })).toBeNull();
    expect(onChoose).not.toHaveBeenCalled();
  });

  it("is remembered per browser, and a stored value it does not know reads as All", () => {
    renderTree();
    showKinds("Editable");
    expect(JSON.parse(window.localStorage.getItem(TABLE_KINDS_STORAGE_KEY) ?? "null")).toBe(
      "editable",
    );
    cleanup();
    renderTree();
    expect(within(kinds()).getByRole("button", { name: "Editable" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(read()).toEqual([
      "Fuel, 2 tables",
      "Fuel Targets",
      "Fuel Compensation",
      "Ignition, 1 table",
      "Timing Main (trim A)",
    ]);
    cleanup();
    window.localStorage.setItem(TABLE_KINDS_STORAGE_KEY, JSON.stringify("everything"));
    renderTree();
    expect(within(kinds()).getByRole("button", { name: "All" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

describe("TableTree below 900px (W10.11)", () => {
  it("is a native select, one optgroup per group, the marks as text, and no filter", () => {
    const { onChoose } = renderTree({
      narrow: true,
      tables: [FUEL_TARGETS, K, FUEL_COMP, BASE_TIMING_TABLE, NO_GROUP],
      pending: pendingOn(FUEL_COMP, 2),
    });
    expect(screen.queryByRole("navigation", { name: "Tables" })).not.toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    const select = screen.getByRole("combobox", { name: "Table" });
    const groups = [...select.querySelectorAll("optgroup")].map((group) => [
      group.label,
      [...group.querySelectorAll("option")].map((option) => option.textContent),
    ]);
    expect(groups).toEqual([
      ["Fuel", ["Fuel Targets ✎", "K (injector constant) ✎", "Fuel Compensation ✎ (2 pending)"]],
      ["Ignition", ["Base timing rate map"]],
      ["Other", ["Mystery table"]],
    ]);
    expect(select).toHaveDisplayValue("Fuel Targets ✎");
    fireEvent.change(select, { target: { value: (select as HTMLSelectElement).options[3].value } });
    expect(onChoose).toHaveBeenCalledWith(BASE_TIMING_TABLE);
  });

  it("names each copy in a family by its slot", () => {
    const { onChoose } = renderTree({ narrow: true, tables: SLOTTED });
    const select = screen.getByRole("combobox", { name: "Table" });
    expect(
      [...select.querySelectorAll("optgroup")[0].querySelectorAll("option")].map(
        (option) => option.textContent,
      ),
    ).toEqual([
      "Fuel Targets · Map 1 ✎",
      "Fuel Targets · Map 2",
      "Fuel Targets · Map 3",
      "Fuel Compensation · Map 1 ✎",
      "Fuel Compensation · Map 2",
      "Fuel Compensation · Map 3",
    ]);
    fireEvent.change(select, { target: { value: (select as HTMLSelectElement).options[4].value } });
    expect(onChoose).toHaveBeenCalledWith(fuelCompTable(2));
  });

  it("applies Editable / View only too, keeping a hidden choice as the select's value", () => {
    renderTree({ narrow: true, tables: SLOTTED, chosen: fuelTargets(2) });
    const select = screen.getByRole("combobox", { name: "Table" });
    showKinds("Editable");
    expect([...select.querySelectorAll("optgroup option")].map((o) => o.textContent)).toEqual([
      "Fuel Targets ✎",
      "Fuel Compensation ✎",
    ]);
    expect(select).toHaveDisplayValue("Fuel Targets · Map 2");
  });
});

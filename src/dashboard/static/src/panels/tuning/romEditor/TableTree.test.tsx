/**
 * The ROM Editor's table picker (W10.10-W10.11): every non-axis table of one
 * slot, grouped as `rom_map.yaml` files them, with a filter, collapsible
 * groups and the marks for editable, pending and an unconfirmed decode; below
 * 900px the same choice as a native select.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  BASE_TIMING_TABLE,
  TABLES,
  TABLES_WITH_CURVES,
  fuelCompTable,
} from "../../../test/romLibraryFixtures";
import type { PendingEdit } from "../../../types/romEditor";
import type { RomTableValues } from "../../../types/romLibrary";
import { TableTree } from "./TableTree";

const [FUEL_TARGETS, K] = TABLES.tables;
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

const TABLES_IN_SLOT = [
  FUEL_TARGETS,
  NO_GROUP,
  TRIM_A,
  FUEL_COMP,
  CURVE,
  BASE_TIMING_TABLE,
  SCALAR,
];

function pendingOn(table: RomTableValues, count: number): PendingEdit[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `${table.id}-${i}`,
    summary: "scale",
    tableName: table.name,
    chain: null,
    edit: { table: table.id ?? "", slot: 1, cells: [[i, 0]], scale: 1.02, rounding: "nearest" },
  }));
}

function renderTree(overrides: Partial<Parameters<typeof TableTree>[0]> = {}): {
  onChoose: ReturnType<typeof vi.fn>;
  onSlot: ReturnType<typeof vi.fn>;
} {
  const onChoose = vi.fn();
  const onSlot = vi.fn();
  render(
    <TableTree
      tables={TABLES_IN_SLOT}
      chosen={FUEL_TARGETS}
      slots={[1]}
      slot={1}
      pending={[]}
      narrow={false}
      onSlot={onSlot}
      onChoose={onChoose}
      {...overrides}
    />,
  );
  return { onChoose, onSlot };
}

const tree = () => screen.getByRole("navigation", { name: "Tables" });
/** Group headings and table names, top to bottom, as assistive tech reads them. */
const read = () =>
  within(tree())
    .getAllByRole("button")
    .map(
      (button) => button.getAttribute("aria-label") ?? button.textContent?.replace(/[✎\d]+$/u, ""),
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
    renderTree({ tables: [...TABLES_IN_SLOT, LONG] });
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
    expect(within(tree()).queryAllByRole("button")).toEqual([]);
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

  it("offers the map slot when there is more than one, and only then", () => {
    const { onSlot } = renderTree({ slots: [1, 2, 3] });
    const select = screen.getByRole("combobox", { name: "Map slot" });
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Map 1 (editable)", "Map 2", "Map 3"]);
    fireEvent.change(select, { target: { value: "3" } });
    expect(onSlot).toHaveBeenCalledWith(3);
  });

  it("has no slot choice for a single slot", () => {
    renderTree();
    expect(screen.queryByRole("combobox", { name: "Map slot" })).not.toBeInTheDocument();
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
});

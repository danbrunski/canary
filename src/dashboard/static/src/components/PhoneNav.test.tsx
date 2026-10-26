import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NAV, PHONE_TABS, moreNavGroups, phoneTabFor } from "../nav";
import type { NavGroupDef, WorkbenchItem } from "../types";
import { PhoneNav } from "./PhoneNav";

function renderNav(overrides: Partial<Parameters<typeof PhoneNav>[0]> = {}): {
  onDrive: ReturnType<typeof vi.fn>;
  onNavigate: ReturnType<typeof vi.fn>;
} {
  const onDrive = vi.fn();
  const onNavigate = vi.fn();
  render(
    <PhoneNav
      mode="parked"
      item="system-info"
      onDrive={onDrive}
      onNavigate={onNavigate}
      {...overrides}
    />,
  );
  return { onDrive, onNavigate };
}

const bar = () => within(screen.getByRole("navigation", { name: "Phone sections" }));

describe("phone navigation model (P2.2)", () => {
  it("puts the operator's five entries on the bar, in order", () => {
    expect(PHONE_TABS.map((tab) => tab.label)).toEqual([
      "Drive",
      "Logging",
      "Alerts",
      "ROM",
      "System",
    ]);
  });

  it("reaches every NAV page exactly once: a bar entry or the More sheet", () => {
    const onBar = PHONE_TABS.flatMap((tab) => tab.items);
    const inMore = moreNavGroups().flatMap((group) => group.items.map((item) => item.id));
    const all = NAV.flatMap((group) => group.items.map((item) => item.id));
    expect([...onBar, ...inMore].sort()).toEqual([...all].sort());
    expect(new Set(onBar).size).toBe(onBar.length);
  });

  it("files a page added to NAV under its group in More, with no phone change", () => {
    const extended: NavGroupDef[] = NAV.map((group) =>
      group.id === "system"
        ? {
            ...group,
            items: [
              ...group.items,
              { id: "system-paired" as WorkbenchItem, label: "Paired devices", group: "system" },
            ],
          }
        : group,
    );
    const system = moreNavGroups(extended).find((group) => group.id === "system");
    expect(system?.items.map((item) => item.label)).toContain("Paired devices");
  });

  it("maps a route to its bar entry; a More page has none", () => {
    expect(phoneTabFor("driving", "diag-explore")?.id).toBe("drive");
    expect(phoneTabFor("parked", "diag-anomalies")?.id).toBe("alerts");
    expect(phoneTabFor("parked", "tuning-rom-library")?.id).toBe("rom");
    expect(phoneTabFor("parked", "tuning-rom-editor")?.id).toBe("rom");
    expect(phoneTabFor("parked", "diag-explore")).toBeNull();
  });
});

describe("PhoneNav", () => {
  it("navigates from the bottom bar and marks the current entry", () => {
    const { onDrive, onNavigate } = renderNav({ item: "diag-anomalies" });
    expect(bar().getByRole("button", { name: "Alerts" })).toHaveAttribute("aria-current", "page");
    fireEvent.click(bar().getByRole("button", { name: "Logging" }));
    expect(onNavigate).toHaveBeenLastCalledWith("tuning-logging");
    fireEvent.click(bar().getByRole("button", { name: "ROM" }));
    expect(onNavigate).toHaveBeenLastCalledWith("tuning-rom-library");
    fireEvent.click(bar().getByRole("button", { name: "Drive" }));
    expect(onDrive).toHaveBeenCalledOnce();
  });

  it("marks Drive current in the driving context", () => {
    renderNav({ mode: "driving" });
    expect(bar().getByRole("button", { name: "Drive" })).toHaveAttribute("aria-current", "page");
    expect(bar().getByRole("button", { name: "System" })).not.toHaveAttribute("aria-current");
  });

  it("opens a More sheet grouped by NAV labels, and closes it on a selection", () => {
    const { onNavigate } = renderNav();
    fireEvent.click(bar().getByRole("button", { name: "More" }));
    const sheet = screen.getByRole("dialog", { name: "More" });
    for (const label of ["Observe", "Engine Tuning", "Diagnose", "Model", "System"]) {
      expect(within(sheet).getByRole("region", { name: label })).toBeInTheDocument();
    }
    // Bar pages are not repeated in the sheet.
    expect(within(sheet).queryByRole("button", { name: "Trouble Codes" })).not.toBeInTheDocument();
    const diagnose = within(within(sheet).getByRole("region", { name: "Diagnose" }));
    fireEvent.click(diagnose.getByRole("button", { name: "Explore" }));
    expect(onNavigate).toHaveBeenLastCalledWith("diag-explore");
    expect(screen.queryByRole("dialog", { name: "More" })).not.toBeInTheDocument();
  });

  it("closes the More sheet on Escape and on the backdrop, returning focus", () => {
    renderNav();
    const more = bar().getByRole("button", { name: "More" });
    more.focus();
    fireEvent.click(more);
    expect(more).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("dialog", { name: "More" })).toHaveFocus();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "More" })).not.toBeInTheDocument();
    expect(more).toHaveFocus();

    fireEvent.click(more);
    fireEvent.click(screen.getByTestId("sheet-backdrop"));
    expect(screen.queryByRole("dialog", { name: "More" })).not.toBeInTheDocument();
  });

  it("shows pages registered after it was written", () => {
    const groups: NavGroupDef[] = [
      {
        id: "system",
        label: "System",
        items: [{ id: "system-network", label: "Network", group: "system" }],
      },
    ];
    const { onNavigate } = renderNav({ groups });
    fireEvent.click(bar().getByRole("button", { name: "More" }));
    fireEvent.click(screen.getByRole("button", { name: "Network" }));
    expect(onNavigate).toHaveBeenLastCalledWith("system-network");
  });

  it("highlights More while a More page is open", () => {
    renderNav({ item: "diag-explore" });
    expect(bar().getByRole("button", { name: "More" })).toHaveAttribute("aria-current", "page");
  });
});

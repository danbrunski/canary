import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NAV } from "../nav";
import { AreaBar } from "./AreaBar";

const bar = () => within(screen.getByRole("navigation", { name: "Workbench areas" }));

describe("AreaBar (W10.2)", () => {
  it("lists every NAV group in order, the settings group last", () => {
    render(<AreaBar active="observe" onChoose={() => {}} />);
    const names = bar()
      .getAllByRole("button")
      .map((button) => button.textContent?.replace("⚙", ""));
    expect(names).toEqual(NAV.map((group) => group.label));
    expect(NAV.at(-1)?.settings).toBe(true);
  });

  it("marks the active area and only it", () => {
    render(<AreaBar active="tuning" onChoose={() => {}} />);
    const current = bar()
      .getAllByRole("button")
      .filter((button) => button.getAttribute("aria-current") === "page");
    expect(current.map((button) => button.textContent)).toEqual(["Engine Tuning"]);
  });

  it("names the gear-marked area by its label alone", () => {
    render(<AreaBar active="observe" onChoose={() => {}} />);
    const system = bar().getByRole("button", { name: "System" });
    expect(system).toHaveTextContent("⚙");
    expect(system.className).toContain("ml-auto");
  });

  it("reports the chosen area", () => {
    const onChoose = vi.fn();
    render(<AreaBar active="observe" onChoose={onChoose} />);
    fireEvent.click(bar().getByRole("button", { name: "Diagnose" }));
    expect(onChoose).toHaveBeenCalledWith("diagnose");
  });
});

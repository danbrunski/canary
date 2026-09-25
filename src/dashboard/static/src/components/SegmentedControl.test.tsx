import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SegmentedControl } from "./SegmentedControl";

const OPTIONS = [
  { id: "table", label: "Table" },
  { id: "visual", label: "Visual" },
] as const;

describe("SegmentedControl", () => {
  it("is a labelled group with exactly the chosen button pressed", () => {
    render(
      <SegmentedControl label="Presentation" options={OPTIONS} value="table" onChange={() => {}} />,
    );
    expect(screen.getByRole("group", { name: "Presentation" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Table" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Visual" })).toHaveAttribute("aria-pressed", "false");
  });

  it("reports the chosen id", () => {
    const onChange = vi.fn();
    render(
      <SegmentedControl label="Presentation" options={OPTIONS} value="table" onChange={onChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Visual" }));
    expect(onChange).toHaveBeenCalledWith("visual");
  });
});

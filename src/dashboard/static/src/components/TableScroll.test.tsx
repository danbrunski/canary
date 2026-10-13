import { createRef } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { keyValueTableClass, tableScrollClass } from "../styles";
import { TableScroll } from "./TableScroll";

describe("TableScroll (P2.5)", () => {
  it("scrolls a wide table sideways in its own box and pins the row name on phones", () => {
    render(
      <TableScroll className="max-h-80 rounded-lg">
        <table aria-label="Wide">
          <tbody>
            <tr>
              <th>row</th>
              <td>value</td>
            </tr>
          </tbody>
        </table>
      </TableScroll>,
    );
    const box = screen.getByRole("table", { name: "Wide" }).parentElement!;
    expect(box).toHaveAttribute("data-table-scroll");
    expect(box).toHaveAttribute("tabindex", "0");
    expect(box.className).toContain("overflow-x-auto");
    expect(box.className).toContain("max-h-80");
    // The sticky first column applies only below 600px; desktop tables are
    // untouched.
    expect(tableScrollClass).toContain("max-[600px]:[&_tr>:first-child]:sticky");
    expect(tableScrollClass).toContain("max-[600px]:[&_tr>:first-child]:left-0");
    expect(tableScrollClass).not.toMatch(/(^| )\[&_tr>:first-child\]:sticky/);
  });

  it("passes a virtualised table's ref and scroll handler through", () => {
    const ref = createRef<HTMLDivElement>();
    const onScroll = vi.fn();
    render(
      <TableScroll ref={ref} onScroll={onScroll} style={{ maxHeight: 100 }}>
        <table />
      </TableScroll>,
    );
    expect(ref.current).toBeInstanceOf(HTMLDivElement);
    expect(ref.current?.style.maxHeight).toBe("100px");
    fireEvent.scroll(ref.current!);
    expect(onScroll).toHaveBeenCalledOnce();
  });

  it("stacks key/value tables into cards only below 600px", () => {
    for (const rule of ["max-[600px]:block", "max-[600px]:[&_tr]:flex-col"]) {
      expect(keyValueTableClass).toContain(rule);
    }
    expect(keyValueTableClass.split(" ").filter((c) => !c.startsWith("max-[600px]:"))).toEqual([
      "w-full",
      "border-collapse",
    ]);
  });
});

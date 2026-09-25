import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Panel, PanelGrid } from "./Panel";

describe("Panel primitive", () => {
  it("renders the title as an h2 (stable for role/text tests)", () => {
    render(createElement(Panel, { title: "System", children: "body" }));
    expect(screen.getByRole("heading", { name: "System" })).toBeInTheDocument();
    expect(screen.getByText("body")).toBeInTheDocument();
  });

  it("renders a count when no actions are given", () => {
    render(createElement(Panel, { title: "Anomalies", count: "3 events", children: null }));
    expect(screen.getByText("3 events")).toBeInTheDocument();
  });

  it("count and actions coexist", () => {
    render(
      createElement(Panel, {
        title: "T",
        count: "3 events",
        actions: createElement("span", null, "filters"),
        children: null,
      }),
    );
    expect(screen.getByText("filters")).toBeInTheDocument();
    expect(screen.getByText("3 events")).toBeInTheDocument();
  });

  it("PanelGrid renders its children", () => {
    render(
      createElement(PanelGrid, {
        cols: 2,
        children: [
          createElement("div", { key: "a" }, "one"),
          createElement("div", { key: "b" }, "two"),
        ],
      }),
    );
    expect(screen.getByText("one")).toBeInTheDocument();
    expect(screen.getByText("two")).toBeInTheDocument();
  });
});

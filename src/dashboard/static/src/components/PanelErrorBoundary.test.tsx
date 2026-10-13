import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PanelErrorBoundary } from "./PanelErrorBoundary";

function Bomb({ explode }: { explode: boolean }) {
  if (explode)
    throw new Error("Cannot read properties of undefined (reading 'keepalive_remaining_s')");
  return <p>panel ok</p>;
}

describe("PanelErrorBoundary", () => {
  afterEach(() => vi.restoreAllMocks());

  it("contains a crash to the panel and names it", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(
      <>
        <p>header still here</p>
        <PanelErrorBoundary resetKey="system-power">
          <Bomb explode />
        </PanelErrorBoundary>
      </>,
    );
    expect(screen.getByText("header still here")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toMatch(/keepalive_remaining_s/);
  });

  it("clears when the user moves to another page", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { rerender } = render(
      <PanelErrorBoundary resetKey="a">
        <Bomb explode />
      </PanelErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeTruthy();
    rerender(
      <PanelErrorBoundary resetKey="b">
        <Bomb explode={false} />
      </PanelErrorBoundary>,
    );
    expect(screen.getByText("panel ok")).toBeTruthy();
  });

  it("retries on request", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    let explode = true;
    function Flaky() {
      return <Bomb explode={explode} />;
    }
    render(
      <PanelErrorBoundary resetKey="a">
        <Flaky />
      </PanelErrorBoundary>,
    );
    explode = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("panel ok")).toBeTruthy();
  });
});

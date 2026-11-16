import { Suspense } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PageLoading } from "../components/PageLoading";
import { PanelErrorBoundary } from "../components/PanelErrorBoundary";
import { ChunkLoadError, lazyChunk } from "./lazyChunk";

function Page({ name }: { name: string }) {
  return <p>page {name}</p>;
}

describe("lazyChunk", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows the loading line, then the page with its props", async () => {
    const Lazy = lazyChunk(() => Promise.resolve({ Page }), "Page");
    render(
      <Suspense fallback={<PageLoading />}>
        <Lazy name="x" />
      </Suspense>,
    );
    expect(screen.getByRole("status").textContent).toMatch(/Loading/);
    expect(await screen.findByText("page x")).toBeTruthy();
  });

  it("memoises the page once when asked", async () => {
    const renders = vi.fn();
    function Counted({ name }: { name: string }) {
      renders();
      return <p>counted {name}</p>;
    }
    const Lazy = lazyChunk(() => Promise.resolve({ Counted }), "Counted", { memoised: true });
    const view = render(
      <Suspense fallback={null}>
        <Lazy name="a" />
      </Suspense>,
    );
    await screen.findByText("counted a");
    const before = renders.mock.calls.length;
    view.rerender(
      <Suspense fallback={null}>
        <Lazy name="a" />
      </Suspense>,
    );
    expect(renders.mock.calls.length).toBe(before);
  });

  it("turns a failed chunk fetch into a recoverable reload prompt, not a blank page", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    const Lazy = lazyChunk(
      () =>
        Promise.reject<{ Page: typeof Page }>(
          new TypeError("Failed to fetch dynamically imported module: /assets/Page-old.js"),
        ),
      "Page",
    );
    render(
      <>
        <p>shell still here</p>
        <PanelErrorBoundary resetKey="p">
          <Suspense fallback={<PageLoading />}>
            <Lazy name="x" />
          </Suspense>
        </PanelErrorBoundary>
      </>,
    );
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/could not be loaded/);
    expect(alert.textContent).toMatch(/Failed to fetch dynamically imported module/);
    expect(screen.getByText("shell still here")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(reload).toHaveBeenCalledOnce();
    vi.unstubAllGlobals();
  });

  it("names the cause on the error it throws", () => {
    const error = new ChunkLoadError(new Error("boom"));
    expect(error.name).toBe("ChunkLoadError");
    expect(error.message).toMatch(/boom/);
  });
});

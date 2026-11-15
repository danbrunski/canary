import { act, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { HttpError } from "../../api/core";
import {
  route,
  runButton,
  setUpSimulateTests,
  simulateCalls,
  state,
  withPendingEdit,
  TIMINGS_KEY,
  field,
} from "../../test/romSimulateKit";
import { click, type } from "../../test/controlRoutes";
import { buildSimResult } from "../../test/romSimFixtures";
import type { RomSimResult } from "../../types/romEditor";

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

describe("Cancel", () => {
  const busy = () =>
    new HttpError(
      409,
      JSON.stringify({
        detail: {
          error: "analysis_busy",
          reason: "child_busy",
          message: "another analysis is running",
        },
      }),
    );

  it("stops waiting, aborts the request, and says the server finishes the run", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    route({ simulate: () => new Promise<RomSimResult>(() => {}) });
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    const running = screen.getByText(/Simulating…/).closest("[role=status]") as HTMLElement;
    // Before anyone clicks: the button and what it does.
    expect(within(running).getByRole("button", { name: "Cancel (stop waiting)" })).toBeVisible();
    expect(running).toHaveTextContent(
      /Cancel only stops waiting: the server finishes this run \(about [\d.]+ s, the dev-laptop fit; not yet measured on this server\) before another can start/,
    );
    expect(running).toHaveTextContent("No route stops it sooner");
    const signal = simulateCalls()[0].init?.signal as AbortSignal;
    expect(signal.aborted).toBe(false);
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    await click(within(running).getByRole("button", { name: "Cancel (stop waiting)" }));
    expect(signal.aborted).toBe(true);
    expect(screen.queryByText(/Simulating…/)).not.toBeInTheDocument();
    const stopped = screen
      .getByText(/You stopped waiting after 2 s/)
      .closest("[role=status]") as HTMLElement;
    expect(stopped).toHaveTextContent("The daemon is still running that simulation");
    expect(stopped).toHaveTextContent(
      "a different simulation is refused as busy (409 analysis_busy)",
    );
    expect(stopped).toHaveTextContent("re-attaches to the job already running");
    // No result, no timing stored from a run nobody saw finish.
    expect(screen.queryByRole("region", { name: "Simulation result" })).not.toBeInTheDocument();
    expect(window.localStorage.getItem(TIMINGS_KEY)).toBeNull();
    expect(runButton(form)).toBeEnabled();
  });

  it("explains the busy 409 that follows as the run that was cancelled", async () => {
    let calls = 0;
    route({
      simulate: () => {
        calls += 1;
        return calls === 1 ? new Promise<RomSimResult>(() => {}) : busy();
      },
    });
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    await click(screen.getByRole("button", { name: "Cancel (stop waiting)" }));
    // A different request: another seed.
    type(field(form, "Seed"), "42");
    await click(runButton(form));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Busy: another analysis is running: child_busy");
    expect(alert).toHaveTextContent(
      "That is probably the run you cancelled: Cancel stops waiting, the daemon kept running it.",
    );
    expect(simulateCalls()).toHaveLength(2);
  });

  it("does not claim a busy 409 is the cancelled run when nothing was cancelled", async () => {
    route({ simulate: busy() });
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent("run you cancelled");
  });

  it("never shows the result of a run that was cancelled, even if it comes back", async () => {
    let resolve: (value: RomSimResult) => void = () => {};
    route({ simulate: () => new Promise<RomSimResult>((done) => (resolve = done)) });
    const form = await withPendingEdit();
    state(form);
    await click(runButton(form));
    await click(screen.getByRole("button", { name: "Cancel (stop waiting)" }));
    await act(async () => {
      resolve(buildSimResult());
    });
    expect(screen.queryByRole("region", { name: "Simulation result" })).not.toBeInTheDocument();
    expect(screen.getByText(/You stopped waiting/)).toBeVisible();
  });
});

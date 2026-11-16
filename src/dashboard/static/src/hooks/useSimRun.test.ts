import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { simulateRomEdits } from "../api/workbench";
import { TIMINGS_STORAGE_KEY } from "../lib/romSimSize";
import { buildRequest, buildSimResult, tablesWithAxes } from "../test/romSimFixtures";
import type { PendingEdit, RomSimResult } from "../types/romEditor";
import type { RomLibraryEntry } from "../types/romLibrary";
import { useSimRun } from "./useSimRun";

vi.mock("../api/workbench", () => ({ simulateRomEdits: vi.fn() }));

const SHA = "b".repeat(64);
const ENTRY = {
  sha256: SHA,
  label: "base",
  filename: "base.bin",
  revision_match: { revision: "ZS44A" },
} as unknown as RomLibraryEntry;
const PENDING: PendingEdit[] = [
  {
    id: "1",
    summary: "scale x1.02",
    edit: {
      table: "fuel_compensation",
      slot: 1,
      cells: [[0, 0]],
      scale: 1.02,
      rounding: "nearest",
    },
    tableName: "Fuel Comp",
    chain: null,
  },
];
const TABLES = tablesWithAxes().tables;

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  let reject: (error: unknown) => void = () => undefined;
  const promise = new Promise<T>((ok, no) => {
    resolve = ok;
    reject = no;
  });
  return { promise, resolve, reject };
}

function mount(edits = PENDING.map((item) => item.edit)) {
  return renderHook(
    (props: { edits: typeof edits }) =>
      useSimRun({
        token: "tok",
        sha: SHA,
        entry: ENTRY,
        baseTables: TABLES,
        pending: PENDING,
        edits: props.edits,
      }),
    { initialProps: { edits } },
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  window.localStorage.clear();
});
afterEach(() => {
  vi.useRealTimers();
  window.localStorage.clear();
});

describe("useSimRun (R28.5)", () => {
  it("runs, lands the result with the run it was for, and remembers how long it took", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.mocked(simulateRomEdits).mockImplementation(async () => {
      vi.advanceTimersByTime(4000);
      return buildSimResult();
    });
    const { result } = mount();
    await act(() => result.current.run(buildRequest()));
    const sim = result.current.sim;
    if (sim.kind !== "ok") throw new Error(`expected ok, got ${sim.kind}`);
    expect(sim.run.base).toEqual({ sha256: SHA, label: "base", revision: "ZS44A" });
    expect(sim.run.edits).toEqual([
      { tableName: "Fuel Comp", summary: "scale x1.02", chain: null },
    ]);
    expect(window.localStorage.getItem(TIMINGS_STORAGE_KEY)).not.toBeNull();
    expect(result.current.timings).toHaveLength(1);
    expect(sim.run.elapsedS).toBe(4);
  });

  it("says a failed run failed, by its reason", async () => {
    vi.mocked(simulateRomEdits).mockRejectedValue(new Error("pi went away"));
    const { result } = mount();
    await act(() => result.current.run(buildRequest()));
    expect(result.current.sim).toEqual({
      kind: "failed",
      failure: { kind: "error", reason: null, message: "pi went away" },
    });
  });

  it("stops waiting on Cancel, aborts the request and never shows the answer", async () => {
    const slow = deferred<RomSimResult>();
    vi.mocked(simulateRomEdits).mockReturnValue(slow.promise);
    const { result } = mount();
    let running: Promise<void> = Promise.resolve();
    act(() => {
      running = result.current.run(buildRequest());
    });
    expect(result.current.sim.kind).toBe("running");
    const signal = vi.mocked(simulateRomEdits).mock.calls[0]?.[3];
    act(() => result.current.cancel());
    expect(signal?.aborted).toBe(true);
    expect(result.current.sim.kind).toBe("cancelled");
    expect(result.current.abandoned).not.toBeNull();
    await act(async () => {
      slow.resolve(buildSimResult());
      await running;
    });
    expect(result.current.sim.kind).toBe("cancelled");
    expect(window.localStorage.getItem(TIMINGS_STORAGE_KEY)).toBeNull();
  });

  it("ignores Cancel when nothing is running", () => {
    const { result } = mount();
    act(() => result.current.cancel());
    expect(result.current.sim.kind).toBe("idle");
    expect(result.current.abandoned).toBeNull();
  });

  it("drops the earlier of two runs when a second starts", async () => {
    const first = deferred<RomSimResult>();
    const second = deferred<RomSimResult>();
    vi.mocked(simulateRomEdits)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const { result } = mount();
    let a: Promise<void> = Promise.resolve();
    let b: Promise<void> = Promise.resolve();
    act(() => {
      a = result.current.run(buildRequest());
    });
    act(() => {
      b = result.current.run({ ...buildRequest(), label: "second" });
    });
    await act(async () => {
      first.reject(new Error("aborted"));
      await a;
    });
    expect(result.current.sim.kind).toBe("running");
    await act(async () => {
      second.resolve(buildSimResult());
      await b;
    });
    expect(result.current.sim.kind).toBe("ok");
  });

  it("aborts the request in flight when the image is left", () => {
    vi.mocked(simulateRomEdits).mockReturnValue(deferred<RomSimResult>().promise);
    const { result, unmount } = mount();
    act(() => {
      void result.current.run(buildRequest());
    });
    const signal = vi.mocked(simulateRomEdits).mock.calls[0]?.[3];
    unmount();
    expect(signal?.aborted).toBe(true);
  });

  it("calls a result stale once the pending edits differ from what it ran", async () => {
    vi.mocked(simulateRomEdits).mockResolvedValue(buildSimResult());
    const same = buildRequest().edits;
    const { result, rerender } = mount(same);
    expect(result.current.stale).toBe(false);
    await act(() => result.current.run(buildRequest()));
    expect(result.current.stale).toBe(false);
    rerender({ edits: [] });
    expect(result.current.stale).toBe(true);
    rerender({ edits: same });
    expect(result.current.stale).toBe(false);
  });
});

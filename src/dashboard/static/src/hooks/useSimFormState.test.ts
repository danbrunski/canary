import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HttpError } from "../api/core";
import { getRomTypicalValues } from "../api/workbench";
import { SIM_STORAGE_KEY } from "../lib/romSim";
import { tablesWithAxes } from "../test/romSimFixtures";
import { SHIPPED_TYPICAL } from "../test/romTypicalFixtures";
import type { RomTypicalValues } from "../types/romEditor";
import { useSimFormState } from "./useSimFormState";

vi.mock("../api/workbench", () => ({ getRomTypicalValues: vi.fn() }));

const SHA = "b".repeat(64);
const TABLES = tablesWithAxes().tables;

function mount() {
  return renderHook(() =>
    useSimFormState({ token: "tok", sha: SHA, tables: TABLES, label: "x", edits: [], timings: [] }),
  );
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((ok) => (resolve = ok));
  return { promise, resolve };
}

beforeEach(() => {
  vi.resetAllMocks();
  window.localStorage.clear();
});
afterEach(() => window.localStorage.clear());

describe("useSimFormState (R28.5)", () => {
  it("fetches the typical values once and fills from the kept answer on the next click", async () => {
    vi.mocked(getRomTypicalValues).mockResolvedValue(SHIPPED_TYPICAL);
    const { result } = mount();
    await act(() => result.current.fillTypical());
    expect(result.current.state.typicalState.kind).toBe("done");
    act(() => result.current.change("baro", "61"));
    await act(() => result.current.fillTypical());
    expect(getRomTypicalValues).toHaveBeenCalledTimes(1);
    expect(getRomTypicalValues).toHaveBeenCalledWith("tok", SHA, expect.any(AbortSignal));
    expect(result.current.state.values.baro).not.toBe("61");
  });

  it("ignores a second click while the values are on their way", async () => {
    const slow = deferred<RomTypicalValues>();
    vi.mocked(getRomTypicalValues).mockReturnValue(slow.promise);
    const { result } = mount();
    let first: Promise<void> = Promise.resolve();
    act(() => {
      first = result.current.fillTypical();
    });
    expect(result.current.state.typicalState.kind).toBe("loading");
    await act(() => result.current.fillTypical());
    expect(getRomTypicalValues).toHaveBeenCalledTimes(1);
    await act(async () => {
      slow.resolve(SHIPPED_TYPICAL);
      await first;
    });
    expect(result.current.state.typicalState.kind).toBe("done");
  });

  it("reads what the operator stated while the values were on their way", async () => {
    const slow = deferred<RomTypicalValues>();
    vi.mocked(getRomTypicalValues).mockReturnValue(slow.promise);
    const { result } = mount();
    let pending: Promise<void> = Promise.resolve();
    act(() => {
      pending = result.current.fillTypical();
    });
    act(() => result.current.change("baro", "61"));
    await act(async () => {
      slow.resolve(SHIPPED_TYPICAL);
      await pending;
    });
    const state = result.current.state.typicalState;
    if (state.kind !== "done") throw new Error("expected done");
    expect(state.fill.filled.find((item) => item.field === "baro_kpa")?.replaced).toBe("61");
  });

  it("leaves the form as it was on a failure, and asks again on the next click", async () => {
    vi.mocked(getRomTypicalValues).mockRejectedValueOnce(new HttpError(409, "{}"));
    const { result } = mount();
    const before = result.current.state.values;
    await act(() => result.current.fillTypical());
    expect(result.current.state.typicalState.kind).toBe("failed");
    expect(result.current.state.values).toBe(before);
    vi.mocked(getRomTypicalValues).mockResolvedValueOnce(SHIPPED_TYPICAL);
    await act(() => result.current.fillTypical());
    expect(getRomTypicalValues).toHaveBeenCalledTimes(2);
    expect(result.current.state.typicalState.kind).toBe("done");
  });

  it("drops an answer that arrives after the form is left", async () => {
    const slow = deferred<RomTypicalValues>();
    vi.mocked(getRomTypicalValues).mockReturnValue(slow.promise);
    const { result, unmount } = mount();
    let pending: Promise<void> = Promise.resolve();
    act(() => {
      pending = result.current.fillTypical();
    });
    const signal = vi.mocked(getRomTypicalValues).mock.calls[0]?.[2];
    unmount();
    expect(signal?.aborted).toBe(true);
    slow.resolve(SHIPPED_TYPICAL);
    await pending;
  });

  it("forgets the remembered values in storage when they are cleared", () => {
    window.localStorage.setItem(
      SIM_STORAGE_KEY,
      JSON.stringify({ version: 1, savedAt: "2026-11-01T10:00:00.000Z", values: { baro: "80" } }),
    );
    const { result } = mount();
    expect(result.current.state.values.baro).toBe("80");
    expect(result.current.state.remembered.has("baro")).toBe(true);
    act(() => result.current.clearRemembered());
    expect(window.localStorage.getItem(SIM_STORAGE_KEY)).toBeNull();
    expect(result.current.state.values.baro).toBe("");
  });

  it("applies a preset only on a base that has breakpoints", () => {
    const { result } = mount();
    act(() => result.current.applyPreset("fine"));
    expect(result.current.state.values.draws).toBe("1000");
    expect(result.current.readout.mode).toBe("fine");
  });
});

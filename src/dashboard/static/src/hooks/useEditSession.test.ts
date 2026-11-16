import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { loadEditorState } from "../lib/romEditor";
import type { PendingEdit } from "../types/romEditor";
import { useEditSession } from "./useEditSession";

const SHA = "b".repeat(64);
const entry = (table: string): Omit<PendingEdit, "id"> => ({
  summary: `scale on ${table}`,
  edit: { table, scale: 1.02 },
  tableName: table,
  chain: null,
});

beforeEach(() => window.localStorage.clear());
afterEach(() => window.localStorage.clear());

describe("useEditSession (R28.5)", () => {
  it("adds an edit with an id of its own and says the edits changed", () => {
    const onEdited = vi.fn();
    const { result } = renderHook(() => useEditSession(SHA, onEdited));
    act(() => {
      result.current.add(entry("fuel_targets"));
      result.current.add(entry("k_constant"));
    });
    expect(result.current.pending).toHaveLength(2);
    const [first, second] = result.current.pending;
    expect(first?.id).not.toBe(second?.id);
    expect(result.current.edits.map((edit) => edit.table)).toEqual(["fuel_targets", "k_constant"]);
    expect(onEdited).toHaveBeenCalledTimes(2);
  });

  it("removes by id and relabels, each saying the edits changed", () => {
    const onEdited = vi.fn();
    const { result } = renderHook(() => useEditSession(SHA, onEdited));
    act(() => result.current.add(entry("fuel_targets")));
    onEdited.mockClear();
    act(() => result.current.setLabel("richer light load"));
    expect(result.current.label).toBe("richer light load");
    expect(onEdited).toHaveBeenCalledTimes(1);
    act(() => result.current.remove(result.current.pending[0]?.id ?? ""));
    expect(result.current.pending).toEqual([]);
    expect(onEdited).toHaveBeenCalledTimes(2);
  });

  it("saves every change for this image and brings it back on the next visit", () => {
    const first = renderHook(() => useEditSession(SHA, vi.fn()));
    act(() => {
      first.result.current.add(entry("fuel_targets"));
      first.result.current.setLabel("kept");
    });
    expect(loadEditorState(SHA).label).toBe("kept");
    expect(loadEditorState(SHA).pending).toHaveLength(1);
    first.unmount();
    const second = renderHook(() => useEditSession(SHA, vi.fn()));
    expect(second.result.current.label).toBe("kept");
    expect(second.result.current.pending.map((item) => item.tableName)).toEqual(["fuel_targets"]);
  });

  it("keeps another image's edits out", () => {
    const first = renderHook(() => useEditSession(SHA, vi.fn()));
    act(() => first.result.current.add(entry("fuel_targets")));
    const other = renderHook(() => useEditSession("c".repeat(64), vi.fn()));
    expect(other.result.current.pending).toEqual([]);
  });
});

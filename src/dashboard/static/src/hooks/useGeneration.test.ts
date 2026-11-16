import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useGeneration, useLatestRequest } from "./useGeneration";

describe("useGeneration (R28.5)", () => {
  it("is the same object across renders and hands out increasing numbers", () => {
    const { result, rerender } = renderHook(() => useGeneration());
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
    expect(first.current()).toBe(0);
    expect(first.next()).toBe(1);
    expect(first.next()).toBe(2);
    expect(first.current()).toBe(2);
  });

  it("makes a token stale once anything claims or bumps after it", () => {
    const { result } = renderHook(() => useGeneration());
    const generation = result.current;
    const mine = generation.next();
    expect(generation.isCurrent(mine)).toBe(true);
    generation.bump();
    expect(generation.isCurrent(mine)).toBe(false);
    const later = generation.next();
    expect(generation.isCurrent(later)).toBe(true);
    generation.next();
    expect(generation.isCurrent(later)).toBe(false);
  });
});

describe("useLatestRequest (R28.5)", () => {
  it("aborts the previous request when a new one starts", () => {
    const { result } = renderHook(() => useLatestRequest());
    const first = result.current.start();
    const second = result.current.start();
    expect(first.signal.aborted).toBe(true);
    expect(result.current.isCurrent(first.token)).toBe(false);
    expect(second.signal.aborted).toBe(false);
    expect(result.current.isCurrent(second.token)).toBe(true);
  });

  it("cancel drops the wait without starting another request", () => {
    const { result } = renderHook(() => useLatestRequest());
    const run = result.current.start();
    result.current.cancel();
    expect(run.signal.aborted).toBe(true);
    expect(result.current.isCurrent(run.token)).toBe(false);
  });

  it("cancels the request in flight when the component unmounts", () => {
    const { result, unmount } = renderHook(() => useLatestRequest());
    const run = result.current.start();
    const request = result.current;
    unmount();
    expect(run.signal.aborted).toBe(true);
    expect(request.isCurrent(run.token)).toBe(false);
  });
});

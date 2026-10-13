import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PHONE_MEDIA_QUERY, useIsPhone, useMediaQuery } from "./useMediaQuery";

afterEach(() => vi.unstubAllGlobals());

describe("useMediaQuery", () => {
  it("defaults to the desktop layout where matchMedia does not exist", () => {
    vi.stubGlobal("matchMedia", undefined);
    expect(renderHook(() => useIsPhone()).result.current).toBe(false);
  });

  it("follows the query as the viewport crosses it", () => {
    let matches = false;
    const listeners = new Set<() => void>();
    vi.stubGlobal("matchMedia", (media: string) => ({
      get matches() {
        return media === PHONE_MEDIA_QUERY && matches;
      },
      media,
      addEventListener: (_: string, listener: () => void) => listeners.add(listener),
      removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
    }));
    const { result, unmount } = renderHook(() => useMediaQuery(PHONE_MEDIA_QUERY));
    expect(result.current).toBe(false);
    act(() => {
      matches = true;
      listeners.forEach((listener) => listener());
    });
    expect(result.current).toBe(true);
    unmount();
    expect(listeners.size).toBe(0);
  });

  it("names the same edge as the max-[600px] utilities", () => {
    // Tailwind's `max-[600px]` is `width < 600px`.
    expect(PHONE_MEDIA_QUERY).toBe("(max-width: 599.98px)");
  });
});

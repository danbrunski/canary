import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CONTROL_TOKEN_KEY } from "../hooks/useControlToken";
import { useHashRoute } from "../hooks/useHashRoute";
import { consumeTokenHandoff } from "./tokenHandoff";

const ROM_EDITOR = "tuning-rom-editor";

function open(hash: string) {
  window.history.replaceState(null, "", `/${hash}`);
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
});

describe("consumeTokenHandoff", () => {
  it("stores the token and leaves the bare route in the URL", () => {
    open(`#wb/${ROM_EDITOR}?token=abc123`);
    expect(consumeTokenHandoff()).toBe(true);
    expect(window.localStorage.getItem(CONTROL_TOKEN_KEY)).toBe("abc123");
    expect(window.location.hash).toBe(`#wb/${ROM_EDITOR}`);
    expect(window.location.href).not.toContain("abc123");
  });

  it("does not add a history entry", () => {
    const before = window.history.length;
    open(`#wb/${ROM_EDITOR}?token=abc123`);
    const afterOpen = window.history.length;
    consumeTokenHandoff();
    expect(window.history.length).toBe(afterOpen);
    expect(afterOpen).toBe(before);
  });

  it("is a no-op without a token", () => {
    window.localStorage.setItem(CONTROL_TOKEN_KEY, "old");
    open(`#wb/${ROM_EDITOR}`);
    expect(consumeTokenHandoff()).toBe(false);
    expect(window.location.hash).toBe(`#wb/${ROM_EDITOR}`);
    expect(window.localStorage.getItem(CONTROL_TOKEN_KEY)).toBe("old");
  });

  it("leaves a hash with other params only untouched", () => {
    open("#wb/system-info?x=1");
    expect(consumeTokenHandoff()).toBe(false);
    expect(window.location.hash).toBe("#wb/system-info?x=1");
  });

  it("leaves the stored token alone for an empty token", () => {
    window.localStorage.setItem(CONTROL_TOKEN_KEY, "old");
    open(`#wb/${ROM_EDITOR}?token=`);
    expect(consumeTokenHandoff()).toBe(false);
    expect(window.localStorage.getItem(CONTROL_TOKEN_KEY)).toBe("old");
    expect(window.location.hash).toBe(`#wb/${ROM_EDITOR}`);
  });

  it("treats a whitespace-only token as empty", () => {
    window.localStorage.setItem(CONTROL_TOKEN_KEY, "old");
    open(`#wb/${ROM_EDITOR}?token=%20%20`);
    expect(consumeTokenHandoff()).toBe(false);
    expect(window.localStorage.getItem(CONTROL_TOKEN_KEY)).toBe("old");
  });

  it("decodes URL-encoded characters", () => {
    open(`#wb/${ROM_EDITOR}?token=a%2Bb%2Fc%3D%20d%26e`);
    consumeTokenHandoff();
    expect(window.localStorage.getItem(CONTROL_TOKEN_KEY)).toBe("a+b/c= d&e");
  });

  it("keeps a literal plus", () => {
    open(`#wb/${ROM_EDITOR}?token=a+b`);
    consumeTokenHandoff();
    expect(window.localStorage.getItem(CONTROL_TOKEN_KEY)).toBe("a+b");
  });

  it("ignores other params, before or after, and the route still parses", () => {
    open(`#wb/${ROM_EDITOR}?x=1&token=tok&y=2`);
    consumeTokenHandoff();
    expect(window.localStorage.getItem(CONTROL_TOKEN_KEY)).toBe("tok");
    expect(window.location.hash).toBe(`#wb/${ROM_EDITOR}`);
  });

  it("only honours the token key", () => {
    open(`#wb/${ROM_EDITOR}?tokens=nope&mytoken=nope`);
    expect(consumeTokenHandoff()).toBe(false);
    expect(window.localStorage.getItem(CONTROL_TOKEN_KEY)).toBeNull();
  });

  it("handles a hash of only #?token=x", () => {
    open("#?token=x");
    expect(consumeTokenHandoff()).toBe(true);
    expect(window.localStorage.getItem(CONTROL_TOKEN_KEY)).toBe("x");
    expect(window.location.hash).toBe("");
    expect(window.location.href).not.toContain("token");
  });

  it("overwrites a stored token", () => {
    window.localStorage.setItem(CONTROL_TOKEN_KEY, "old");
    open(`#wb/${ROM_EDITOR}?token=new`);
    consumeTokenHandoff();
    expect(window.localStorage.getItem(CONTROL_TOKEN_KEY)).toBe("new");
  });

  it("still cleans the URL when localStorage is unavailable", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    open(`#wb/${ROM_EDITOR}?token=abc`);
    expect(() => consumeTokenHandoff()).not.toThrow();
    expect(window.location.hash).toBe(`#wb/${ROM_EDITOR}`);
  });

  it("preserves the path and query string", () => {
    window.history.replaceState(null, "", `/app/?a=1#wb/${ROM_EDITOR}?token=abc`);
    consumeTokenHandoff();
    expect(window.location.pathname + window.location.search + window.location.hash).toBe(
      `/app/?a=1#wb/${ROM_EDITOR}`,
    );
  });

  it("keeps the route: useHashRoute resolves the item after the handoff", () => {
    open(`#wb/${ROM_EDITOR}?token=abc`);
    consumeTokenHandoff();
    const { result } = renderHook(() => useHashRoute());
    expect(result.current.mode).toBe("parked");
    expect(result.current.item).toBe(ROM_EDITOR);
  });
});

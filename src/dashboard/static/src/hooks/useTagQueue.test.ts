import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { mockHttp } from "../test/harness";
import type { CaptureState } from "../types";

const mocks = mockHttp(["tagCapture"]);

const { useTagQueue } = await import("./useTagQueue");

const QUEUE_KEY = "canary.testQueue.v1";
const CAPTURE = { capture_id: "cap-1" } as CaptureState;

function row(id: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    captureId: "cap-1",
    label: "pull_start",
    kind: "event",
    t: 100,
    attempts: 0,
    status: "sent",
    ...extra,
  };
}

describe("useTagQueue persistence (R08.3)", () => {
  let restore: (() => void) | null = null;

  beforeEach(() => {
    window.localStorage.clear();
    mocks.tagCapture.mockResolvedValue({});
  });

  afterEach(() => {
    restore?.();
    restore = null;
  });

  it("restores the persisted recent log and drops malformed rows", () => {
    window.localStorage.setItem(
      QUEUE_KEY,
      JSON.stringify({ queue: [], recent: [row("a"), { id: 3 }, "junk", row("b")] }),
    );
    const { result } = renderHook(() => useTagQueue("t", CAPTURE));
    expect(result.current.recent.map((r) => r.id)).toEqual(["a", "b"]);
    expect(result.current.tapCounts.get("pull_start")).toBe(2);
  });

  it("starts empty on unparseable storage", () => {
    window.localStorage.setItem(QUEUE_KEY, "{not json");
    const { result } = renderHook(() => useTagQueue("t", CAPTURE));
    expect(result.current.queue).toEqual([]);
    expect(result.current.recent).toEqual([]);
  });

  it("queues in memory when localStorage throws", () => {
    const original = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException("The operation is insecure.", "SecurityError");
      },
    });
    restore = () => {
      if (original) Object.defineProperty(window, "localStorage", original);
    };
    mocks.tagCapture.mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useTagQueue("t", CAPTURE));
    act(() => result.current.enqueue("brake", "event"));
    expect(result.current.recent.map((r) => r.label)).toEqual(["brake"]);
    expect(result.current.pendingCount).toBe(1);
  });

  it("persists an enqueued tag", () => {
    mocks.tagCapture.mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useTagQueue("t", CAPTURE));
    act(() => result.current.enqueue("brake", "event"));
    const stored = JSON.parse(window.localStorage.getItem(QUEUE_KEY) ?? "{}") as {
      queue: Array<{ label: string }>;
    };
    expect(stored.queue.map((q) => q.label)).toEqual(["brake"]);
  });
});

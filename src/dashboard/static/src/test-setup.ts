import "@testing-library/jest-dom/vitest";
import { afterEach, beforeAll, vi } from "vitest";
import { cleanup, configure } from "@testing-library/react";

// Testing-library's default 1 s `waitFor`/`findBy*` budget is tight when
// vitest runs one jsdom worker per core and a loaded machine starves a
// worker mid-wait. Widen to just under vitest's 5 s testTimeout so a
// slow-but-passing wait doesn't false-negative, while a genuine hang still
// fails inside testing-library (with its DOM dump) rather than as an
// opaque vitest timeout. Healthy waits return the moment the condition
// holds, so this costs nothing when green.
configure({ asyncUtilTimeout: 4500 });

// jsdom 25 has no PointerEvent, so `fireEvent.pointerDown(el, { pointerType:
// "touch", clientX })` would build a plain Event and drop both fields. The
// charts read pointers, not mouse events (phone touch readout, P4.2).
if (typeof window.PointerEvent === "undefined") {
  class PointerEventPolyfill extends MouseEvent {
    readonly pointerId: number;
    readonly pointerType: string;
    readonly isPrimary: boolean;
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init);
      this.pointerId = init.pointerId ?? 1;
      this.pointerType = init.pointerType ?? "mouse";
      this.isPrimary = init.isPrimary ?? true;
    }
  }
  window.PointerEvent = PointerEventPolyfill as unknown as typeof PointerEvent;
}

beforeAll(() => {
  Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
    configurable: true,
    value: vi.fn(() => ({
      arc: vi.fn(),
      beginPath: vi.fn(),
      clearRect: vi.fn(),
      // Filled envelopes (Curve's deviation bands) close their path. Every
      // 2D-context method the app calls belongs here: a gap makes the panel
      // throw under test only, which reads as a product bug that isn't one —
      // or worse, hides one that is.
      closePath: vi.fn(),
      fill: vi.fn(),
      fillRect: vi.fn(),
      fillText: vi.fn(),
      lineTo: vi.fn(),
      moveTo: vi.fn(),
      restore: vi.fn(),
      save: vi.fn(),
      scale: vi.fn(),
      setLineDash: vi.fn(),
      setTransform: vi.fn(),
      stroke: vi.fn(),
      strokeRect: vi.fn(),
    })),
  });
});

// Auto-unmount any rendered tree between tests so a stray hook in test A
// doesn't carry state into test B.
afterEach(() => {
  cleanup();
});

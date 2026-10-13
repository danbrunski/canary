import { useEffect, useRef, useState, type RefObject } from "react";

/** Width in CSS pixels of the element behind the returned ref, tracked with a
 *  ResizeObserver (P2.5: canvases size from their container, so a 640 px
 *  chart in a 380 px phone column shrinks instead of being clipped).
 *
 *  `null` until the first observation, and always where ResizeObserver does
 *  not exist (jsdom); callers fall back to their requested size. The element
 *  must take its width from its parent (a block, `w-full`), never from the
 *  canvas inside it, or the measurement feeds back into itself. */
export function useContainerWidth<T extends HTMLElement>(): [RefObject<T>, number | null] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState<number | null>(null);
  useEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    // ResizeObserver reports once as soon as it starts observing, so there
    // is no separate first measurement to take here.
    const observer = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0]?.contentRect.width ?? 0);
      if (next > 0) setWidth((current) => (current === next ? current : next));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

/** The drawing width for a canvas that should fill its container but never
 *  exceed `requested` (when given). */
export function fitWidth(
  measured: number | null,
  requested: number | undefined,
  fallback: number,
): number {
  if (measured === null) return requested ?? fallback;
  return requested === undefined ? measured : Math.min(requested, measured);
}

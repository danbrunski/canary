import { useCallback, useSyncExternalStore } from "react";

/** The phone layout's edge (docs/roadmap/archive/phone-ui.md P2.2). It names the
 *  same width as every `max-[600px]:` utility: Tailwind's `max-[600px]`
 *  compiles to `width < 600px`, which this query matches to the sub-pixel. */
export const PHONE_MEDIA_QUERY = "(max-width: 599.98px)";

function query(media: string): MediaQueryList | null {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return null;
  return window.matchMedia(media);
}

/** Whether `media` matches right now, re-rendering when it flips (a phone
 *  rotating, a desktop window dragged narrow). False where `matchMedia` does
 *  not exist (jsdom, SSR), so the desktop layout is the default. */
export function useMediaQuery(media: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const list = query(media);
      if (!list) return () => {};
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    [media],
  );
  const snapshot = useCallback(() => query(media)?.matches ?? false, [media]);
  return useSyncExternalStore(subscribe, snapshot, () => false);
}

/** True below 600px: bottom bar, condensed header, phone page layouts. */
export function useIsPhone(): boolean {
  return useMediaQuery(PHONE_MEDIA_QUERY);
}

/** The Workbench sidebar's edge, every `max-[900px]:` utility's width: below
 *  it the sidebar is a horizontal strip and does not collapse (W10.4). */
export const NARROW_MEDIA_QUERY = "(max-width: 899.98px)";

/** True below 900px. */
export function useIsNarrow(): boolean {
  return useMediaQuery(NARROW_MEDIA_QUERY);
}

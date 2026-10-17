import type { Page } from "@playwright/test";

/**
 * "No horizontal page scroll", checked two ways (P2.6). The document must not
 * be wider than the viewport. That alone passes trivially, because the shell
 * clips its own overflow (`h-dvh overflow-hidden`), so it also reports every
 * visible element that pokes past the right edge, unless it sits inside a box
 * that scrolls sideways on purpose (a TableScroll, a chip strip): content
 * clipped off-screen is as unreachable as content scrolled off it.
 */
export async function overflowReport(page: Page) {
  return page.evaluate(() => {
    const viewport = window.innerWidth;
    const scrollsSideways = (el: Element) => {
      const style = getComputedStyle(el);
      return (
        (style.overflowX === "auto" || style.overflowX === "scroll") &&
        el.scrollWidth > el.clientWidth
      );
    };
    const offenders: string[] = [];
    for (const el of Array.from(document.body.querySelectorAll("*"))) {
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) continue;
      if (rect.right <= viewport + 1) continue;
      let parent = el.parentElement;
      let contained = false;
      while (parent) {
        if (scrollsSideways(parent)) {
          contained = true;
          break;
        }
        parent = parent.parentElement;
      }
      if (contained) continue;
      const label = el.getAttribute("aria-label") ?? el.textContent?.trim().slice(0, 40) ?? "";
      offenders.push(`${el.tagName.toLowerCase()} right=${Math.round(rect.right)} "${label}"`);
    }
    return {
      viewport,
      documentWidth: document.scrollingElement?.scrollWidth ?? 0,
      offenders: offenders.slice(0, 10),
    };
  });
}

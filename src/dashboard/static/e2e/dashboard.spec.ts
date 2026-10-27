import { expect, test } from "@playwright/test";

import { mockDaemon } from "./fixtures/mockDaemon";

/**
 * End-to-end smoke tests: the real production bundle, in Chromium, against a
 * stubbed daemon (see `fixtures/mockDaemon.ts`). These assert the app boots
 * and the top-level Drive↔Workbench shell behaves — the plumbing that jsdom
 * component tests can't cover (real routing, real WebSocket handshakes, the
 * actual built bundle). Panel internals stay the vitest suite's job.
 *
 * Every journey also runs in the `phone` project (P2.6, 412x915). There the
 * bottom bar replaces the header's mode toggle and the sidebar, and the
 * theme toggle lives in the header menu, so the journeys take that route.
 */

test.beforeEach(async ({ page }) => {
  await mockDaemon(page);
});

test("boots into the drive screen with the daemon connected", async ({ page, isMobile }) => {
  await page.goto("/");

  await expect(page.getByText("CANary", { exact: true })).toBeVisible();
  // The mode subtitle under the brand reads "drive" on first load.
  await expect(page.getByText("drive", { exact: true })).toBeVisible();
  if (isMobile) {
    const bar = page.getByRole("navigation", { name: "Phone sections" });
    await expect(bar.getByRole("button", { name: "Drive" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    return;
  }
  // The mode toggle offers the *other* mode.
  await expect(page.getByRole("button", { name: "switch to the parked workbench" })).toBeVisible();
});

test("toggles between the drive screen and the parked workbench", async ({ page, isMobile }) => {
  await page.goto("/");

  if (isMobile) {
    const bar = page.getByRole("navigation", { name: "Phone sections" });
    await bar.getByRole("button", { name: "System" }).tap();
    await expect(page).toHaveURL(/#wb\/system-info/);
    await expect(page.getByText("workbench", { exact: true })).toBeVisible();
    await bar.getByRole("button", { name: "Drive" }).tap();
    await expect(page).toHaveURL(/#drive/);
    return;
  }

  await page.getByRole("button", { name: "switch to the parked workbench" }).click();

  // Landing on the workbench: the area bar and the area's page nav appear
  // and the URL hash reflects the parked context so a kiosk reload returns
  // here.
  const nav = page.getByRole("navigation", { name: "Workbench sections" });
  const areas = page.getByRole("navigation", { name: "Workbench areas" });
  await expect(nav).toBeVisible();
  await expect(areas.getByRole("button", { name: "Observe" })).toBeVisible();
  await expect(areas.getByRole("button", { name: "Diagnose" })).toBeVisible();
  await expect(page).toHaveURL(/#wb/);

  // And back to driving.
  await page.getByRole("button", { name: "switch to the driving screen" }).click();
  await expect(page).toHaveURL(/#drive/);
  await expect(nav).toBeHidden();
});

test("deep-links straight into a workbench section via the URL hash", async ({
  page,
  isMobile,
}) => {
  await page.goto("/#wb/diag-anomalies");

  if (isMobile) {
    // The page belongs to the Alerts entry, with its sibling pages beside it.
    const bar = page.getByRole("navigation", { name: "Phone sections" });
    await expect(bar.getByRole("button", { name: "Alerts" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    await expect(page.getByRole("navigation", { name: "Alerts pages" })).toBeVisible();
  } else {
    await expect(page.getByRole("navigation", { name: "Workbench sections" })).toBeVisible();
  }
  // Kiosk reload preserves the deep-linked view rather than snapping to drive.
  await expect(page).toHaveURL(/#wb\/diag-anomalies/);
});

test("switches area from the area bar and returns to its last page", async ({ page, isMobile }) => {
  test.skip(isMobile, "the phone has its bottom bar instead (P2.2)");
  await page.goto("/#wb/tuning-rom-editor");
  const areas = page.getByRole("navigation", { name: "Workbench areas" });
  const nav = page.getByRole("navigation", { name: "Workbench sections" });
  await expect(nav.getByRole("button", { name: "ROM Editor" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  // Only the area's pages are listed.
  await expect(nav.getByRole("button", { name: "Anomalies" })).toHaveCount(0);

  await areas.getByRole("button", { name: "System" }).click();
  await expect(page).toHaveURL(/#wb\/system-data-sources/);
  await expect(nav.getByRole("button", { name: "System Info" })).toBeVisible();

  // Back to Engine Tuning lands where it was left, across a reload too.
  await page.reload();
  await areas.getByRole("button", { name: "Engine Tuning" }).click();
  await expect(page).toHaveURL(/#wb\/tuning-rom-editor/);

  // Collapsed, the sidebar keeps only its expand button; the areas remain.
  await nav.getByRole("button", { name: "Collapse sidebar" }).click();
  await expect(nav.getByRole("button", { name: "ROM Editor" })).toHaveCount(0);
  await expect(areas.getByRole("button", { name: "Diagnose" })).toBeVisible();
  await nav.getByRole("button", { name: "Expand sidebar" }).click();
  await expect(nav.getByRole("button", { name: "ROM Editor" })).toBeVisible();
});

test("between 600 and 900px the sidebar strip keeps its pages visible on a tall page", async ({
  page,
  isMobile,
}) => {
  // Regression (W10): the strip is a scroll container in a grid row, and a
  // tall panel below it squeezed that row to its 17px of padding. The strip
  // kept its own height and spilled under the panel, so the area's pages were
  // in the DOM but covered. jsdom has no layout.
  test.skip(isMobile, "the phone has its bottom bar instead (P2.2)");
  await page.setViewportSize({ width: 800, height: 500 });
  await page.goto("/#wb/system-info");
  const nav = page.getByRole("navigation", { name: "Workbench sections" });
  await expect(nav.getByRole("button", { name: "Data Sources" })).toBeVisible();
  const layout = await nav.evaluate((strip) => {
    const first = strip.querySelector("button") as HTMLElement;
    const pane = strip.nextElementSibling as HTMLElement;
    const box = first.getBoundingClientRect();
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return {
      stripBottom: strip.getBoundingClientRect().bottom,
      paneTop: pane.getBoundingClientRect().top,
      uncovered: hit !== null && first.contains(hit),
    };
  });
  expect(layout.paneTop).toBeGreaterThanOrEqual(layout.stripBottom - 1);
  expect(layout.uncovered).toBe(true);
});

test("switches theme from the header toggle", async ({ page, isMobile }) => {
  await page.goto("/");
  if (isMobile) await page.getByRole("button", { name: "open menu" }).tap();

  const toDay = page.getByRole("button", { name: "switch to the day theme" });
  const toNight = page.getByRole("button", {
    name: /switch to the night/,
  });

  // Exactly one of the two theme labels is present at any time; clicking flips it.
  const startOnDark = await toDay.count();
  if (startOnDark) {
    await toDay.click();
    await expect(toNight).toBeVisible();
  } else {
    await toNight.click();
    await expect(toDay).toBeVisible();
  }
});

test("overflowing panel content makes the workbench pane scroll, not clip", async ({ page }) => {
  // Regression: the scroll pane must not itself be the panel grid. A grid
  // that is the scroll container has a definite height, which sizes its auto
  // row to the pane rather than to the content; panels are `overflow-hidden`
  // (panelClass), so the under-sized row makes a tall panel clip its own
  // content while the pane still reports no overflow — content past the fold
  // with no scrollbar and no way to reach it. jsdom has no layout, so only a
  // real browser catches this. (A desktop-width viewport in both projects.)
  await page.setViewportSize({ width: 1280, height: 360 });
  await page.goto("/#wb/diag-anomalies");

  const nav = page.getByRole("navigation", { name: "Workbench sections" });
  await expect(nav).toBeVisible();

  const pane = await nav.evaluate((el) => {
    const p = el.nextElementSibling as HTMLElement;
    const panel = p.querySelector("section, article, div > div") as HTMLElement;
    return {
      scrolls: p.scrollHeight > p.clientHeight,
      panelClips: panel.scrollHeight > panel.clientHeight + 1,
    };
  });

  expect(pane.scrolls).toBe(true);
  expect(pane.panelClips).toBe(false);
});

test("a phone-width workbench pane scrolls, not clips", async ({ page, isMobile }) => {
  // The same regression in the phone layout, whose pane is a different
  // element (no sidebar beside it).
  test.skip(!isMobile, "phone layout only");
  await page.setViewportSize({ width: 412, height: 420 });
  await page.goto("/#wb/diag-anomalies");
  const strip = page.getByRole("navigation", { name: "Alerts pages" });
  await expect(strip).toBeVisible();
  const pane = await strip.evaluate((el) => {
    const p = el.nextElementSibling as HTMLElement;
    const panel = p.querySelector("section") as HTMLElement;
    return {
      scrolls: p.scrollHeight > p.clientHeight,
      panelClips: panel.scrollHeight > panel.clientHeight + 1,
    };
  });
  expect(pane.scrolls).toBe(true);
  expect(pane.panelClips).toBe(false);
});

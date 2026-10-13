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

  // Landing on the workbench: grouped section nav appears and the URL hash
  // reflects the parked context so a kiosk reload returns here.
  const nav = page.getByRole("navigation", { name: "Workbench sections" });
  await expect(nav).toBeVisible();
  await expect(nav.getByText("Observe", { exact: true })).toBeVisible();
  await expect(nav.getByText("Diagnose", { exact: true })).toBeVisible();
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

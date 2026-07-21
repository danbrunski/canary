import { expect, test } from "@playwright/test";

import { mockDaemon } from "./fixtures/mockDaemon";

/**
 * End-to-end smoke tests: the real production bundle, in Chromium, against a
 * stubbed daemon (see `fixtures/mockDaemon.ts`). These assert the app boots
 * and the top-level Drive↔Workbench shell behaves — the plumbing that jsdom
 * component tests can't cover (real routing, real WebSocket handshakes, the
 * actual built bundle). Panel internals stay the vitest suite's job.
 */

test.beforeEach(async ({ page }) => {
  await mockDaemon(page);
});

test("boots into the drive screen with the daemon connected", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByText("CANary", { exact: true })).toBeVisible();
  // The mode subtitle under the brand reads "drive" on first load.
  await expect(page.getByText("drive", { exact: true })).toBeVisible();
  // The mode toggle offers the *other* mode.
  await expect(
    page.getByRole("button", { name: "switch to the parked workbench" }),
  ).toBeVisible();
});

test("toggles between the drive screen and the parked workbench", async ({ page }) => {
  await page.goto("/");

  await page
    .getByRole("button", { name: "switch to the parked workbench" })
    .click();

  // Landing on the workbench: grouped section nav appears and the URL hash
  // reflects the parked context so a kiosk reload returns here.
  const nav = page.getByRole("navigation", { name: "Workbench sections" });
  await expect(nav).toBeVisible();
  await expect(nav.getByText("Live", { exact: true })).toBeVisible();
  await expect(nav.getByText("Diagnostics", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#wb/);

  // And back to driving.
  await page
    .getByRole("button", { name: "switch to the driving screen" })
    .click();
  await expect(page).toHaveURL(/#drive/);
  await expect(nav).toBeHidden();
});

test("deep-links straight into a workbench section via the URL hash", async ({
  page,
}) => {
  await page.goto("/#wb/diag-anomalies");

  await expect(
    page.getByRole("navigation", { name: "Workbench sections" }),
  ).toBeVisible();
  // Kiosk reload preserves the deep-linked view rather than snapping to drive.
  await expect(page).toHaveURL(/#wb\/diag-anomalies/);
});

test("switches theme from the header toggle", async ({ page }) => {
  await page.goto("/");

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

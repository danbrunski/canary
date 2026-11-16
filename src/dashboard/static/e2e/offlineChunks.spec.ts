import { expect, test } from "@playwright/test";

import { mockDaemon } from "./fixtures/mockDaemon";

/**
 * R28.6: the Workbench and the Engine Tuning pages are lazy chunks, and the
 * real service worker (`public/sw.js`) is what keeps them reachable when the
 * Pi is down. This runs the production bundle with the worker registered:
 *
 *  - the Workbench chunk and then every Engine Tuning chunk are prefetched at
 *    idle after load and cached by the worker (operator, 2026-11-15), so the
 *    Workbench and a tuning page never opened while online both open offline.
 *
 * A chunk that is missing (a stale tab after a Pi update) still ends in the
 * Reload notice: `src/lib/lazyChunk.test.tsx` and
 * `tests/test_dashboard_api.py::test_a_vanished_asset_is_a_404_not_the_index`.
 */

test.beforeEach(async ({ page }) => {
  await mockDaemon(page);
});

test("offline: the Workbench and an unvisited tuning page open from the service-worker cache", async ({
  page,
  context,
}) => {
  await page.goto("/");
  await expect(page.getByText("drive", { exact: true })).toBeVisible();

  // The worker registers on `load` and claims the page once active.
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null, undefined, {
    timeout: 20_000,
  });
  // The idle prefetch lands the Workbench chunk and every tuning chunk in the
  // worker's cache (CalibrationPage is fetched last).
  const cached = (pattern: RegExp) =>
    page.evaluate(async (source) => {
      const wanted = new RegExp(source);
      const keys = await Promise.all(
        (await caches.keys()).map(async (name) =>
          (await (await caches.open(name)).keys()).map((request) => request.url),
        ),
      );
      return keys.flat().some((url) => wanted.test(url));
    }, pattern.source);
  for (const chunk of ["Workbench", "LoggingPage", "RomEditorPage", "CalibrationPage"]) {
    await expect
      .poll(() => cached(new RegExp(`/assets/${chunk}-[^/]+\\.js$`)), {
        timeout: 30_000,
        message: `${chunk} chunk never reached the service-worker cache`,
      })
      .toBe(true);
  }

  await context.setOffline(true);

  // Workbench, offline: the chunk comes from the worker's cache.
  await page.getByRole("button", { name: "switch to the parked workbench" }).click();
  await expect(page.getByRole("navigation", { name: "Workbench sections" })).toBeVisible();

  // A tuning page nobody opened while online: its chunk was prefetched too.
  await page.evaluate(() => {
    window.location.hash = "#wb/tuning-calibration";
  });
  await expect(page.getByText("Record a flash")).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "could not be loaded" })).toHaveCount(0);
});

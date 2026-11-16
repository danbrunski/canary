import { expect, test } from "@playwright/test";

import { mockDaemon } from "./fixtures/mockDaemon";

/**
 * R28.6: the Workbench and the Engine Tuning pages are lazy chunks, and the
 * real service worker (`public/sw.js`) is what keeps them reachable when the
 * Pi is down. This runs the production bundle with the worker registered:
 *
 *  - the Workbench chunk is prefetched at idle after load and cached by the
 *    worker, so the Workbench opens offline;
 *  - an Engine Tuning page never opened while online was never cached, so
 *    offline it shows the recoverable "could not be loaded" alert with a
 *    Reload button, not a blank page.
 *
 * Prefetching the tuning chunks too is an open operator choice (~100 kB of
 * background Wi-Fi), so this asserts the on-demand behaviour as shipped.
 */

test.beforeEach(async ({ page }) => {
  await mockDaemon(page);
});

test("offline: the Workbench opens from the service-worker cache, an unvisited tuning page asks for a reload", async ({
  page,
  context,
}) => {
  await page.goto("/");
  await expect(page.getByText("drive", { exact: true })).toBeVisible();

  // The worker registers on `load` and claims the page once active.
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null, undefined, {
    timeout: 20_000,
  });
  // The idle prefetch of the Workbench chunk lands in the shell cache.
  await expect
    .poll(
      () =>
        page.evaluate(async () => {
          const keys = await Promise.all(
            (await caches.keys()).map(async (name) =>
              (await (await caches.open(name)).keys()).map((request) => request.url),
            ),
          );
          return keys.flat().some((url) => /\/assets\/Workbench-[^/]+\.js$/.test(url));
        }),
      { timeout: 30_000, message: "Workbench chunk never reached the service-worker cache" },
    )
    .toBe(true);

  await context.setOffline(true);

  // Workbench, offline: the chunk comes from the worker's cache.
  await page.getByRole("button", { name: "switch to the parked workbench" }).click();
  await expect(page.getByRole("navigation", { name: "Workbench sections" })).toBeVisible();

  // A tuning page nobody opened while online has no cached chunk.
  await page.evaluate(() => {
    window.location.hash = "#wb/tuning-calibration";
  });
  const alert = page.getByRole("alert").filter({ hasText: "could not be loaded" });
  await expect(alert).toBeVisible();
  await expect(alert.getByRole("button", { name: "Reload" })).toBeVisible();
  // The shell around it is still there.
  await expect(page.getByRole("navigation", { name: "Workbench areas" })).toBeVisible();
});

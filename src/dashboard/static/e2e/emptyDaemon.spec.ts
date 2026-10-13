import { expect, test } from "@playwright/test";

import { NAV } from "../src/nav";
import { mockDaemon } from "./fixtures/mockDaemon";

/**
 * Every page survives a daemon whose answers it does not recognise.
 *
 * The mock answers each endpoint it has no fixture for with `{}`, which is
 * what a page sees when the daemon is older or newer than the dashboard: the
 * normal state for a minute after a software update from the phone (P3.1),
 * while a cached shell talks to a restarted daemon. A page may show an empty
 * or error state then; it must not crash. PanelErrorBoundary keeps a crash
 * from blanking the app, and this spec keeps the boundary from being the only
 * thing between a field rename and a dead page.
 */

const PAGES = NAV.flatMap((group) => group.items)
  .filter((item) => !item.planned)
  .map((item) => item.id);

for (const signedIn of [false, true]) {
  // Signed in, the authenticated panels (power, capture, update) fetch too.
  test(`no page crashes on a daemon that answers {} (${signedIn ? "with" : "without"} a token)`, async ({
    page,
  }) => {
    test.setTimeout(120_000);
    const crashes: string[] = [];
    page.on("pageerror", (error) => crashes.push(`pageerror: ${error.message}`));
    if (signedIn) {
      await page.addInitScript(() => localStorage.setItem("canary.controlToken", "ui-test"));
    }
    await mockDaemon(page);
    await page.goto("/#drive");
    await expect(page.locator("body")).not.toBeEmpty();
    for (const id of PAGES) {
      await page.goto(`/#wb/${id}`);
      // Long enough for the page's first fetches to land and render.
      await page.waitForTimeout(400);
      const alert = page.getByText("This page hit an error and was stopped");
      if (await alert.count()) {
        crashes.push(`${id}: ${(await alert.first().textContent()) ?? ""}`);
      }
    }
    expect(crashes).toEqual([]);
  });
}

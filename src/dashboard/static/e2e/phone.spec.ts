import { expect, test, type Page } from "@playwright/test";

import {
  DESCRIPTION,
  ENTRY_K106,
  ENTRY_TUNED,
  TABLES,
  backupState,
} from "../src/test/romLibraryFixtures";
import type { DtcReportResponse, PowerStatusResponse } from "../src/types";
import { mockDaemon } from "./fixtures/mockDaemon";
import { overflowReport } from "./fixtures/overflow";
import { mockWorkbench } from "./fixtures/workbenchRoutes";

/**
 * P2.6 — the phone layout's regression net (docs/roadmap/archive/phone-ui.md). Runs
 * in the `phone` project only: a Pixel 7 in portrait, 412x915, touch on.
 *
 * "No horizontal page scroll" is checked two ways. The document must not be
 * wider than the viewport. That alone passes trivially here, because the
 * shell clips its own overflow (`h-dvh overflow-hidden`), so it also checks
 * that nothing visible pokes past the right edge of the screen, unless it sits
 * inside a box that scrolls sideways on purpose (a TableScroll, a chip strip):
 * content clipped off-screen is as unreachable as content scrolled off it.
 */

const BOTTOM_BAR_PAGES = [
  { tab: "Drive", hash: "#drive" },
  { tab: "Logging", hash: "#wb/tuning-logging" },
  { tab: "Alerts", hash: "#wb/diag-dtc" },
  { tab: "ROM", hash: "#wb/tuning-rom-library" },
  { tab: "System", hash: "#wb/system-info" },
] as const;

const NOW = new Date().toISOString();
const SEED_ROWS = [
  ["RPM", 2412, "rpm"],
  ["SPEED", 64, "km/h"],
  ["COOLANT_TEMP", 91, "°C"],
  ["MAF", 18.4, "g/s"],
  ["THROTTLE_POS", 22.7, "%"],
  ["CONTROL_MODULE_VOLTAGE", 14.1, "V"],
  ["ENGINE_LOAD", 38.2, "%"],
  ["INTAKE_TEMP", 31, "°C"],
].map(([channel, value, unit]) => ({ timestamp: NOW, source: "obd", channel, value, unit }));

const DTC_REPORT: DtcReportResponse = {
  captured_at: NOW,
  stored: [
    {
      code: "P0171",
      description: "System Too Lean (Bank 1)",
      system: "fuel and air metering",
      generic: true,
      curated: true,
    },
  ],
  pending: [],
  permanent: [],
  mil_on: true,
  dtc_count: 1,
  freeze_frame: null,
  stale: false,
};

const POWER: PowerStatusResponse = {
  power: {
    watchdog_running: true,
    watchdog_stale: false,
    armed: true,
    input_lost: false,
    on_battery: false,
    discharging: false,
    discharge_rate_pct_per_hour: 0,
    min_discharge_rate_pct_per_hour: 6,
    soc_critical: false,
    critical_soc_pct: 15,
    shutdown_state: "input_present",
    countdown_reason: null,
    soc: 96,
    vcell: 4.12,
    seconds_lost: null,
    grace_s: 60,
    keepalive_remaining_s: 0,
    status_age_s: 1.2,
  },
};

let pageErrors: string[] = [];

test.beforeEach(async ({ page }) => {
  pageErrors = [];
  // A render error unmounts the whole app; name it instead of timing out on
  // a bottom bar that is no longer there.
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await mockDaemon(page);
  // Live values for the Drive tiles (the WebSocket mock sends nothing).
  await page.route("**/telemetry/latest?*", (route) =>
    route.fulfill({ json: { rows: SEED_ROWS } }),
  );
  // Real shapes where a page would otherwise render an empty state: the
  // trouble-code report, and a ROM library wide enough to need its scroller.
  await page.route(/\/obd\/dtc(\?|$)/, (route) => route.fulfill({ json: DTC_REPORT }));
  await page.route(/\/control\/rom\/library(\?|$)/, (route) =>
    route.fulfill({ json: { entries: [ENTRY_TUNED, ENTRY_K106], count: 2 } }),
  );
  await page.route(/\/control\/rom\/library\/[0-9a-f]+(\?|$)/, (route) =>
    route.fulfill({ json: DESCRIPTION }),
  );
  await page.route(/\/control\/rom\/library\/[0-9a-f]+\/tables(\?|$)/, (route) =>
    route.fulfill({ json: TABLES }),
  );
  await page.route(/\/control\/rom\/backup(\?|$)/, (route) =>
    route.fulfill({ json: backupState() }),
  );
  await page.route(/\/control\/power(\?|$)/, (route) => route.fulfill({ json: POWER }));
});

for (const { tab, hash } of BOTTOM_BAR_PAGES) {
  test(`${tab} page has no horizontal page scroll at 412px`, async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("canary.controlToken", "ui-test"));
    await page.goto(`/${hash}`);
    // Let polled panels settle into whatever they render with the stubs.
    await page.waitForLoadState("networkidle");
    expect(pageErrors).toEqual([]);
    const bar = page.getByRole("navigation", { name: "Phone sections" });
    await expect(bar.getByRole("button", { name: tab })).toHaveAttribute("aria-current", "page");
    const report = await overflowReport(page);
    expect(report.documentWidth).toBeLessThanOrEqual(report.viewport);
    expect(report.offenders).toEqual([]);
  });
}

test("the bottom bar and the More sheet navigate", async ({ page }) => {
  await page.goto("/#drive");
  const bar = page.getByRole("navigation", { name: "Phone sections" });
  await expect(bar).toBeVisible();
  // The desktop sidebar and mode toggle are gone at this width.
  await expect(page.getByRole("navigation", { name: "Workbench sections" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /switch to the/ })).toHaveCount(0);

  await bar.getByRole("button", { name: "Alerts" }).tap();
  await expect(page).toHaveURL(/#wb\/diag-dtc$/);
  // Nor does a Workbench page draw the desktop area bar (W10.2).
  await expect(page.getByRole("navigation", { name: "Workbench areas" })).toHaveCount(0);
  await page
    .getByRole("navigation", { name: "Alerts pages" })
    .getByRole("button", { name: "Anomalies" })
    .tap();
  await expect(page).toHaveURL(/#wb\/diag-anomalies$/);

  await bar.getByRole("button", { name: "More" }).tap();
  const sheet = page.getByRole("dialog", { name: "More" });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByRole("region", { name: "Diagnose" })).toBeVisible();
  await sheet.getByRole("button", { name: "Explore" }).tap();
  await expect(sheet).toBeHidden();
  await expect(page).toHaveURL(/#wb\/diag-explore$/);
  await expect(bar.getByRole("button", { name: "More" })).toHaveAttribute("aria-current", "page");

  // Escape and the backdrop close it too.
  await bar.getByRole("button", { name: "More" }).tap();
  await expect(sheet).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
  await bar.getByRole("button", { name: "More" }).tap();
  await page.getByTestId("sheet-backdrop").tap({ position: { x: 20, y: 20 } });
  await expect(sheet).toBeHidden();

  await bar.getByRole("button", { name: "Drive" }).tap();
  await expect(page).toHaveURL(/#drive$/);
});

test("the header menu holds the theme toggle and the control token", async ({ page }) => {
  await page.goto("/#drive");
  await expect(page.getByRole("status", { name: /^connection: / })).toBeVisible();
  await page.getByRole("button", { name: "open menu" }).tap();
  const menu = page.getByRole("dialog", { name: "Menu" });
  await expect(menu.getByRole("button", { name: "control token" })).toBeVisible();
  await expect(menu.getByRole("button", { name: /switch to the/ })).toBeVisible();
});

test("the Drive screen shows two large tiles a row with finger-sized pins", async ({ page }) => {
  await page.goto("/#drive");
  const pin = page.getByRole("button", { name: /^(un)?pin Rpm$/i });
  await expect(pin).toBeVisible();
  const box = await pin.boundingBox();
  expect(box?.width).toBeGreaterThanOrEqual(32);
  expect(box?.height).toBeGreaterThanOrEqual(32);

  const tiles = await page
    .getByRole("button", { name: /^(un)?pin / })
    .evaluateAll((pins) =>
      pins.map((p) => Math.round((p.parentElement!.parentElement as HTMLElement).offsetLeft)),
    );
  expect(new Set(tiles).size).toBe(2);

  // Pinning from the phone persists per device.
  const pinnedCount = () =>
    page.evaluate(
      () => (JSON.parse(localStorage.getItem("canary.drive.pinned") ?? "[]") as string[]).length,
    );
  const before = await pinnedCount();
  const unpinned = page.getByRole("button", { name: /^pin / }).first();
  const name = (await unpinned.getAttribute("aria-label"))!.replace(/^pin /, "");
  await unpinned.tap();
  await expect(page.getByRole("button", { name: `unpin ${name}` })).toBeVisible();
  await expect.poll(pinnedCount).toBe(before + 1);
});

/**
 * R19: the Logs and Analysis pages at 412 px, with real-shaped data. The
 * service worker is blocked because it answers `/analysis/*` GETs itself
 * (that prefix is missing from its dynamic list), which hides them from the
 * route stubs.
 */
test.describe("Logs and Analysis at 412px", () => {
  test.use({ serviceWorkers: "block" });

  const sideways = async (page: Page) => {
    const report = await overflowReport(page);
    expect(report.documentWidth).toBeLessThanOrEqual(report.viewport);
    expect(report.offenders).toEqual([]);
  };

  test("a long log is windowed, scrolls inside its own box, and the page does not scroll sideways", async ({
    page,
  }) => {
    await mockWorkbench(page, { rows: 2000 });
    await page.goto("/#wb/tuning-logs");
    const table = page.getByRole("table", { name: "Log table" });
    await expect(table).toBeVisible();
    await page.waitForLoadState("networkidle");
    expect(pageErrors).toEqual([]);

    // A window of rows, never the 2000.
    const rows = table.locator("tr[data-row]");
    expect(await rows.count()).toBeLessThanOrEqual(58);
    const box = page.locator("[data-table-scroll]").filter({ has: table });
    await box.evaluate((el) => {
      el.scrollTop = el.scrollHeight;
    });
    await expect(rows.last()).toContainText("20990");
    expect(await rows.count()).toBeLessThanOrEqual(58);
    await sideways(page);
  });

  test("the visual presentation fits the screen", async ({ page }) => {
    await mockWorkbench(page, { rows: 300 });
    await page.goto("/#wb/tuning-logs");
    await expect(page.getByRole("table", { name: "Log table" })).toBeVisible();
    await page.getByRole("button", { name: "Visual" }).click();
    await expect(page.getByLabel("Scatter")).toBeVisible();
    await sideways(page);
    await page.getByRole("button", { name: "Heat map" }).click();
    await expect(page.getByLabel("Heat map")).toBeVisible();
    await sideways(page);
  });

  test("the delivery chart of a few bands fits the screen", async ({ page }) => {
    await mockWorkbench(page, { bands: 2 });
    await page.goto("/#wb/tuning-analysis");
    await page.getByRole("button", { name: /All on the running calibration/ }).click();
    await page.getByLabel("Measured-AFR source").selectOption("obd:O2_WR_LAMBDA_S1");
    await page.getByRole("button", { name: "Run" }).click();
    await expect(page.getByLabel("Delivery error by rpm band")).toBeVisible();
    await sideways(page);
  });

  // Six bands are 420 px of chart on a 412 px screen: the chart scrolls inside its own
  // box instead of widening the page.
  test("the delivery chart of six bands fits the screen", async ({ page }) => {
    await mockWorkbench(page, { bands: 6 });
    await page.goto("/#wb/tuning-analysis");
    await page.getByRole("button", { name: /All on the running calibration/ }).click();
    await page.getByLabel("Measured-AFR source").selectOption("obd:O2_WR_LAMBDA_S1");
    await page.getByRole("button", { name: "Run" }).click();
    await expect(page.getByLabel("Delivery error by rpm band")).toBeVisible();
    await sideways(page);
  });
});

/** N5.4: saved WiFi networks on System -> Network, driven at 412 px against a
 *  stateful stub of `/control/network/wifi`. */
test.describe("Saved WiFi networks at 412px", () => {
  const HOME = {
    id: "id-home",
    ssid: "Home-Starlink-With-A-Rather-Long-Network-Name",
    priority: 100,
    autoconnect: true,
    hidden: false,
    security: "wpa-psk",
    active: true,
    in_range: true,
    signal: 81,
    managed_by: "canary",
  };

  test("adds a network from the scan with Join now, no sideways scroll", async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("canary.controlToken", "ui-test"));
    const networks = [HOME];
    const scan = [
      { ssid: HOME.ssid, signal: 80, security: "WPA2", saved: true },
      { ssid: "Neighbour", signal: 44, security: "WPA2", saved: false },
    ];
    const posted: unknown[] = [];
    const wifi = () => ({ networks, scan, scanned_at: 1_700_000_000, interface: "wlan0" });
    await page.route(/\/control\/network(\?|$)/, (route) =>
      route.fulfill({
        json: {
          network: {
            hostname: "canary-1a2b",
            mode: "client",
            ssid: HOME.ssid,
            wifi_interface: "wlan0",
            addresses: { wlan0: ["192.168.1.23"] },
            dashboard_url: "http://192.168.1.23:8000/",
            read_at: 1_700_000_000,
            age_s: 0,
            error: null,
          },
          provisioning: { window_remaining_s: 0, max_minutes: 30 },
        },
      }),
    );
    await page.route(/\/control\/network\/wifi(\?|$)/, async (route) => {
      if (route.request().method() === "POST") {
        const body = route.request().postDataJSON() as { ssid: string };
        posted.push(body);
        const added = {
          ...HOME,
          id: "id-new",
          ssid: body.ssid,
          priority: 110,
          active: true,
          signal: 44,
        };
        networks.forEach((n) => (n.active = false));
        networks.unshift(added);
        return route.fulfill({ json: { network: added, wifi: wifi() } });
      }
      return route.fulfill({ json: { wifi: wifi() } });
    });

    await page.goto("/#wb/system-network");
    await expect(page.getByRole("heading", { name: "Saved WiFi networks" })).toBeVisible();
    await expect(page.getByRole("list", { name: "Saved networks" })).toContainText("connected");

    await page.getByRole("button", { name: "choose Neighbour" }).tap();
    await page.getByLabel("Passphrase").fill("correct horse battery");
    await page.getByRole("button", { name: "Join now" }).tap();

    await expect(page.getByText("Joined Neighbour.")).toBeVisible();
    expect(posted).toEqual([
      { ssid: "Neighbour", password: "correct horse battery", hidden: false, join: true },
    ]);
    await expect(page.getByLabel("Passphrase")).toHaveValue("");
    const first = page.getByRole("list", { name: "Saved networks" }).getByRole("listitem").first();
    await expect(first).toContainText("Neighbour");
    await expect(first).toContainText("connected");

    expect(pageErrors).toEqual([]);
    const report = await overflowReport(page);
    expect(report.documentWidth).toBeLessThanOrEqual(report.viewport);
    expect(report.offenders).toEqual([]);
  });
});

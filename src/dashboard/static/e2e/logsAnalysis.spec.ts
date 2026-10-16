import { expect, test, type Page } from "@playwright/test";

import { mockWorkbench } from "./fixtures/workbenchRoutes";

/**
 * R19: the Logs table windows its rows, scrolls inside its own box, and the
 * plots are sized as drawn, in a real browser (jsdom has no layout, so the
 * vitest suite scrolls by assigning `scrollTop` and cannot see any of this).
 * The 412 px phone layout is checked in phone.spec.ts with the same data.
 *
 * The service worker is blocked: it answers `/analysis/*` GETs itself (that
 * prefix is missing from its dynamic list), which would hide them from the
 * stubs below. See the R19 close-out.
 */
test.use({ serviceWorkers: "block" });

const ROW_HEIGHT = 24;
const VIEWPORT_ROWS = 18;
const WINDOW = VIEWPORT_ROWS + 2 * 20;
const ROWS = 2000;

let pageErrors: string[] = [];
test.beforeEach(async ({ page }) => {
  pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await mockWorkbench(page, { rows: ROWS, bands: 6 });
});
test.afterEach(() => expect(pageErrors).toEqual([]));

const logTable = (page: Page) => page.getByRole("table", { name: "Log table" });
const scroller = (page: Page) =>
  page.locator("[data-table-scroll]").filter({ has: logTable(page) });
const dataRows = (page: Page) => logTable(page).locator("tr[data-row]");

test("a long log renders a window of rows inside a scroll box of its own", async ({ page }) => {
  await page.goto("/#wb/tuning-logs");
  await expect(logTable(page)).toBeVisible();

  // A window of the 2000 rows, not all of them.
  const rendered = await dataRows(page).count();
  expect(rendered).toBeGreaterThanOrEqual(VIEWPORT_ROWS);
  expect(rendered).toBeLessThanOrEqual(WINDOW);
  await expect(dataRows(page).first()).toContainText("1000");

  // The box is capped at 18 rows plus the header and scrolls vertically; the
  // spacer rows give it the height of every row.
  const metrics = await scroller(page).evaluate((el) => ({
    client: el.clientHeight,
    scroll: el.scrollHeight,
    overflowY: getComputedStyle(el).overflowY,
    tabIndex: el.tabIndex,
  }));
  expect(metrics.client).toBeLessThanOrEqual(ROW_HEIGHT * VIEWPORT_ROWS + 28);
  expect(metrics.scroll).toBeGreaterThan(ROW_HEIGHT * ROWS);
  expect(metrics.overflowY).toBe("auto");
  expect(metrics.tabIndex).toBe(0);
});

test("scrolling moves the window and the last row is reachable", async ({ page }) => {
  await page.goto("/#wb/tuning-logs");
  await expect(logTable(page)).toBeVisible();

  await scroller(page).evaluate((el) => {
    el.scrollTop = el.scrollHeight;
  });
  // rpm of the last row is 1000 + 1999 * 10.
  await expect(dataRows(page).last()).toContainText("20990");
  await expect(dataRows(page).first()).not.toContainText("1000");
  expect(await dataRows(page).count()).toBeLessThanOrEqual(WINDOW);

  // And back: the first row returns, still a window.
  await scroller(page).evaluate((el) => {
    el.scrollTop = 0;
  });
  await expect(dataRows(page).first()).toContainText("1000");
  expect(await dataRows(page).count()).toBeLessThanOrEqual(WINDOW);
});

test("sorting descending puts the last row first, still windowed", async ({ page }) => {
  await page.goto("/#wb/tuning-logs");
  await expect(logTable(page)).toBeVisible();
  const header = logTable(page).getByRole("button", { name: /obd:RPM/ });
  await header.click();
  await header.click();
  await expect(dataRows(page).first()).toContainText("20990");
  expect(await dataRows(page).count()).toBeLessThanOrEqual(WINDOW);
});

test("the visual presentation sizes its strips to the panel and its scatter as drawn", async ({
  page,
}) => {
  await page.goto("/#wb/tuning-logs");
  await expect(logTable(page)).toBeVisible();
  await page.getByRole("button", { name: "Visual" }).click();

  const strips = page.getByLabel("Time-series strips").locator("svg");
  await expect(strips).toHaveCount(2);
  const panel = await page.getByLabel("Time-series strips").boundingBox();
  for (let i = 0; i < 2; i++) {
    const box = await strips.nth(i).boundingBox();
    expect(box?.height).toBeCloseTo(64, 0);
    expect(box?.width).toBeCloseTo(panel?.width ?? 0, 0);
  }
  const scatter = await page.getByLabel("Scatter").boundingBox();
  expect(scatter?.width).toBeCloseTo(360, 0);
  expect(scatter?.height).toBeCloseTo(240, 0);
  await page.getByRole("button", { name: "Heat map" }).click();
  const heat = await page.getByLabel("Heat map").boundingBox();
  expect(heat?.width).toBeCloseTo(360, 0);
  expect(heat?.height).toBeCloseTo(240, 0);
});

test("the delivery chart is as wide as its bands need", async ({ page }) => {
  await page.goto("/#wb/tuning-analysis");
  await page.getByRole("button", { name: /All on the running calibration/ }).click();
  await page.getByLabel("Measured-AFR source").selectOption("obd:O2_WR_LAMBDA_S1");
  await page.getByRole("button", { name: "Run" }).click();
  const chart = page.getByLabel("Delivery error by rpm band");
  await expect(chart).toBeVisible();
  // Six bands at 70 each, 200 high plus the label strip.
  const box = await chart.boundingBox();
  expect(box?.width).toBeCloseTo(420, 0);
  expect(box?.height).toBeCloseTo(220, 0);
  await expect(chart.locator("[data-band]")).toHaveCount(6);
});

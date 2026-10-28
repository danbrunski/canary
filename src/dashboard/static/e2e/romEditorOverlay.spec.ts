import { expect, test } from "@playwright/test";

import { mapColor } from "../src/lib/romMaps";
import { ENTRY_TUNED, SHA_TUNED, TABLES_ALL_2D } from "../src/test/romLibraryFixtures";
import { mockDaemon } from "./fixtures/mockDaemon";
import { mockRomApi } from "./fixtures/romEditorApi";
import type { RomTraceContext } from "../src/types/workbench";

/**
 * What the ROM Maps page's browser spec covered, on the ROM Editor it was
 * merged into (2026-10-26): the picker, a colored target table, the live trace
 * and slot navigation; plus the retired page's old address landing on the
 * editor. The daemon is stubbed at the network layer, as in `romEditor.spec.ts`.
 */
const trace: RomTraceContext = {
  sha256: SHA_TUNED,
  map_slot: 1,
  afr_stoich: 14.7,
  recorded_revision: "ZS44A-20260920",
  matches_recorded_revision: true,
  warnings: ["Inferred lookup only; ECU overrides are not modeled."],
  live: {
    rpm: { spec: "did:RPM", transform: "identity", factor: null },
    base_fuel_schedule: { spec: "did:BFS", transform: "identity", factor: null },
    commanded_afr: null,
  },
  tables: [
    {
      kind: "fuel_targets",
      title: "Target AFR",
      x_label: "Base Fuel Schedule",
      x_unit: "ms",
      y_label: "Rotations Per Minute",
      y_unit: "rpm",
      unit: "AFR",
      trace: {
        x: {
          channel: "mem:ECM_FUEL_TARGET_MAP_X_TRACE",
          axis_mapping: "verified",
          axis_units: "raw",
          scale: 1,
          offset: 0,
          evidence: "firmware",
          request_channel: "mem:fuel_axis",
          stored_decode: { scale: 0.250859, offset: 0 },
        },
        y: {
          channel: "mem:ECM_ENGINE_SPEED_RAM",
          axis_mapping: "verified",
          axis_units: "display",
          scale: 1,
          offset: 0,
          evidence: "rpm",
          request_channel: "mem:spark",
          stored_decode: { scale: 12.5, offset: 0 },
        },
        unavailable_note: "No capture profile polls the fuel-target trace channels.",
        out_of_range: { behaviour: "clamp", evidence: "0x026F40" },
      },
    },
  ],
};

async function open(page: import("@playwright/test").Page, target: string) {
  await mockDaemon(page, {
    status: {
      vehicle_state: {
        state: "engine_running",
        rpm: 2800,
        coolant_temp: 85,
        run_time: 200,
        obd_freshness_seconds: 0,
        can_freshness_seconds: 0,
        replay_id: null,
        derived_at: new Date().toISOString(),
      },
    },
  });
  await page.route("**/telemetry/latest?*", (route) =>
    route.fulfill({
      json: {
        rows: [
          {
            timestamp: new Date().toISOString(),
            source: "did",
            channel: "RPM",
            value: 2800,
            unit: "rpm",
          },
          {
            timestamp: new Date().toISOString(),
            source: "did",
            channel: "BFS",
            value: 14,
            unit: "ms",
          },
        ],
      },
    }),
  );
  const api = await mockRomApi(page, {
    library: [ENTRY_TUNED],
    tables: () => TABLES_ALL_2D,
    trace: () => trace,
  });
  await page.addInitScript(() => localStorage.setItem("canary.controlToken", "ui-test"));
  await page.setViewportSize({ width: 1440, height: 1200 });
  await page.goto(target);
  return api;
}

test("colored target table, live overlay behind a toggle, slot navigation", async ({
  page,
}, testInfo) => {
  const api = await open(page, `/?rom=${SHA_TUNED}#wb/tuning-rom-editor`);
  const grid = page.getByRole("grid", { name: "Fuel Targets (Map 1) cells" });
  await expect(grid).toBeVisible();
  await expect(grid.getByRole("gridcell")).toHaveCount(64);
  // Shaded low to high on the shared ramp: the leanest cell is the red end.
  await expect(grid.getByRole("gridcell").first()).toHaveCSS(
    "background-color",
    "rgb(230, 80, 65)",
  );
  const last = grid.getByRole("gridcell").last();
  await expect(last).toHaveCSS("background-color", mapColor(1881.6 / 149, 1881.6 / 149, 14.7));
  // Nothing is traced, and nothing fetched, until the overlay is switched on.
  await expect(grid.locator("[data-trace]")).toHaveCount(0);
  expect(api.calls.some((call) => call.path.endsWith("/trace"))).toBe(false);

  await page.getByRole("checkbox", { name: /^Live overlay/ }).check();
  await page.getByRole("checkbox", { name: /I confirm/ }).check();
  await expect(grid.locator('[data-trace="nearest"]')).toHaveCount(1);
  // The mapping is verified, but nothing polls the trace channels: the trace
  // greys out rather than freezing, and no ECM cell is drawn.
  await expect(grid.locator("[data-ecm]")).toHaveCount(0);
  await expect(page.getByText(/ECM trace greyed out/)).toBeVisible();
  await expect(page.getByText(/ECM cell not drawn/)).toHaveCount(0);
  await expect(page.locator("[data-trace-liveness]")).not.toHaveAttribute(
    "data-trace-liveness",
    "live",
  );
  await grid.screenshot({ path: testInfo.outputPath("rom-editor-overlay-grid.png") });
  await page.screenshot({
    path: testInfo.outputPath("rom-editor-overlay-desktop.png"),
    fullPage: true,
  });

  // Another slot's copy is for looking at: the overlay has no cell to place
  // there. The chosen table's family is open, so Map 2 is one click away.
  const targets = page
    .getByRole("navigation", { name: "Tables" })
    .getByRole("group", { name: "Fuel Targets", exact: true });
  await targets.getByRole("button", { name: "Map 2", exact: true }).click();
  await expect(page.getByRole("table", { name: "Fuel Targets (Map 2) cells" })).toBeVisible();
  await expect(page.getByText(/edits and the simulator cover Map 1 only/)).toBeVisible();
  await expect(page.getByText(/Fuel Targets in Map 2 has none/)).toBeVisible();
  // Below 900px the copies are options of the table select.
  await page.setViewportSize({ width: 430, height: 900 });
  await page
    .getByRole("combobox", { name: "Table" })
    .selectOption({ label: "Fuel Targets · Map 1 ✎" });
  await expect(grid).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("rom-editor-overlay-mobile.png"),
    fullPage: true,
  });
  expect(api.unexpected).toEqual([]);
});

test("the retired ROM Maps address lands on the ROM Editor", async ({ page }) => {
  await open(page, "/#wb/tuning-rom-maps");
  await expect(page.getByRole("heading", { name: "ROM Editor" })).toBeVisible();
  await expect(page).toHaveURL(/#wb\/tuning-rom-editor$/);
  await expect(page.getByRole("button", { name: "ROM Maps" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "ROM Editor", exact: true })).toHaveAttribute(
    "aria-current",
    "page",
  );
});

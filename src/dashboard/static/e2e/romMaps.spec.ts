import { expect, test } from "@playwright/test";
import { mockDaemon } from "./fixtures/mockDaemon";
import type { RomInspection } from "../src/lib/romMaps";

test("ROM picker, colored target table, live trace and five-slot TBD navigation", async ({ page }, testInfo) => {
  const inspection: RomInspection = {
    source_sha256: "a".repeat(64), revision: "synthetic-ui-test", recorded_revision: "synthetic-ui-test",
    matches_recorded_revision: true, map_slots: [1, 2, 3, 4, 5], afr_stoich: 14.7,
    live: { rpm: { spec: "did:RPM", transform: "identity", factor: null }, base_fuel_schedule: { spec: "did:BFS", transform: "identity", factor: null }, commanded_afr: null },
    warnings: ["Inferred lookup only; ECU overrides are not modeled."],
    tables: [1, 2, 3, 4, 5].flatMap((slot) => (["fuel_targets", "fuel_compensation"] as const).map((kind) => ({
      slot, kind, title: kind === "fuel_targets" ? "Target AFR" : "Fuel Compensation",
      status: slot === 1 && kind === "fuel_targets" ? "available" as const : "tbd" as const,
      reason: "TBD — complete table location and axes not verified.",
      x_label: kind === "fuel_targets" ? "Base Fuel Schedule" : "Volumetric Flow Ratio", x_unit: kind === "fuel_targets" ? "ms" : "%",
      y_label: "Rotations Per Minute", y_unit: "rpm", unit: kind === "fuel_targets" ? "AFR" : "%",
      x_axis: [8, 10, 12, 14, 16, 18, 20, 22], y_axis: [1200, 1600, 2000, 2800, 3600, 4000, 4400, 5200],
      raw: Array.from({ length: 8 }, (_, r) => Array.from({ length: 8 }, (_, c) => 128 + r * 3 + c)),
      values: Array.from({ length: 8 }, (_, r) => Array.from({ length: 8 }, (_, c) => 1881.6 / (128 + r * 3 + c))),
      // W05.1 as shipped: the X trace's mapping onto BFS is unverified.
      trace: slot === 1 && kind === "fuel_targets" ? {
        x: { channel: "mem:ECM_FUEL_TARGET_MAP_X_TRACE", axis_mapping: "unverified" as const, scale: null, offset: null, evidence: "Not settled.", request_channel: "mem:fuel_axis" },
        y: { channel: "mem:ECM_ENGINE_SPEED_RAM", axis_mapping: "verified" as const, scale: 1, offset: 0, evidence: "rpm", request_channel: "mem:spark" },
        unavailable_note: "No capture profile polls the fuel-target trace channels.",
      } : null,
    }))),
  };
  await mockDaemon(page, { status: { vehicle_state: {
    state: "engine_running", rpm: 2800, coolant_temp: 85, run_time: 200,
    obd_freshness_seconds: 0, can_freshness_seconds: 0, replay_id: null, derived_at: new Date().toISOString(),
  } } });
  await page.route("**/telemetry/latest?*", (route) => route.fulfill({ json: { rows: [
    { timestamp: new Date().toISOString(), source: "did", channel: "RPM", value: 2800, unit: "rpm" },
    { timestamp: new Date().toISOString(), source: "did", channel: "BFS", value: 14, unit: "ms" },
  ] } }));
  await page.route("**/control/rom/inspect", async (route) => {
    expect(route.request().headers()["content-type"]).toBe("application/octet-stream");
    await route.fulfill({ json: inspection });
  });
  await page.addInitScript(() => localStorage.setItem("canary.controlToken", "ui-test"));
  await page.setViewportSize({ width: 1440, height: 1200 });
  await page.goto("/#wb/tuning-rom-maps");
  await page.getByLabel("ROM file").setInputFiles({ name: "synthetic.OsirisROM", mimeType: "application/octet-stream", buffer: Buffer.from("synthetic ROM") });
  const table = page.getByRole("table", { name: "Map 1 Target AFR values" });
  await expect(table).toBeVisible();
  await expect(table.locator("td")).toHaveCount(64);
  await page.getByRole("checkbox", { name: /I confirm/ }).check();
  await expect(table.locator('[data-trace="nearest"]')).toHaveCount(1);
  // The ECM cell is refused by name while its mapping is unverified, and the
  // trace greys out rather than freezing when nothing polls its channels.
  await expect(table.locator("[data-ecm]")).toHaveCount(0);
  await expect(page.getByText(/ECM cell not drawn: how the X trace/)).toBeVisible();
  await expect(page.locator("[data-trace-liveness]")).not.toHaveAttribute("data-trace-liveness", "live");
  await expect(table.locator("td").first()).toHaveCSS("background-color", "rgb(230, 80, 65)");
  await page.screenshot({ path: testInfo.outputPath("rom-maps-desktop.png"), fullPage: true });
  await page.getByLabel("Map slot").selectOption("5");
  await expect(page.getByRole("region", { name: "Map 5 Fuel Compensation" })).toContainText("TBD");
  await expect(page.getByRole("region", { name: "Map 5 Target AFR" })).toContainText("TBD");
  await expect(page.getByRole("checkbox", { name: /I confirm/ })).not.toBeChecked();
  await page.setViewportSize({ width: 430, height: 900 });
  await page.getByLabel("Map slot").selectOption("1");
  await expect(table).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("rom-maps-mobile.png"), fullPage: true });
});

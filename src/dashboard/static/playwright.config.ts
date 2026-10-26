import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright end-to-end tests for the dashboard.
 *
 * These drive the *production* bundle in a real Chromium, with the daemon's
 * HTTP + WebSocket surface stubbed at the network layer (see
 * `e2e/fixtures/mockDaemon.ts`). That keeps the suite hermetic — no Pi, no CAN
 * hardware, no running Python daemon — while still exercising the real
 * rendered app rather than jsdom. The vitest suite covers component logic in
 * isolation; this covers "does the whole thing boot and render in a browser".
 *
 * The web server builds once (`pnpm build`) and serves `dist/` via
 * `vite preview`, so what runs is exactly what ships.
 */
const PORT = 4173;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  // A stray `.only` left in a spec fails CI rather than silently skipping the rest.
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
      testIgnore: /phone\.spec\.ts$/,
    },
    {
      // P2.6: the regression net for the phone layout (docs/roadmap/
      // phone-ui.md P2-P5). A Pixel 7 in portrait at its full 412x915 screen
      // (the descriptor's own viewport subtracts browser chrome), touch and
      // mobile viewport semantics on. Runs the dashboard journeys plus the
      // phone-only spec; the ROM Editor specs size their own viewports.
      name: "phone",
      use: {
        ...devices["Pixel 7"],
        viewport: { width: 412, height: 915 },
        isMobile: true,
        hasTouch: true,
      },
      testMatch: /(dashboard|phone)\.spec\.ts$/,
    },
  ],
  webServer: {
    command: `pnpm build && pnpm exec vite preview --port ${PORT} --strictPort`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: !process.env.CI,
    // A cold `tsc -b && vite build` from scratch can take a while on CI.
    timeout: 180_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});

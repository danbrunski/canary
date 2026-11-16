/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { gzipSync } from "node:zlib";
import type { Plugin } from "vite";

// What a phone loads before the Drive screen can draw: the entry chunk and every
// chunk it imports statically, gzipped (what crosses the Pi's Wi-Fi). The whole
// app used to be one 235 KB gzip chunk; the Workbench and Engine Tuning pages
// are now lazy (R28.6) and the entry measured 73 KB. The budget leaves room to
// grow but fails the build when a static import drags a page back in; raise it
// on purpose, with the reason, never to make a build pass.
export const ENTRY_GZIP_BUDGET_BYTES = 90_000;

function entryBudget(): Plugin {
  return {
    name: "canary-entry-budget",
    generateBundle(_options, bundle) {
      const seen = new Set<string>();
      const walk = (name: string): number => {
        const chunk = bundle[name];
        if (seen.has(name) || chunk?.type !== "chunk") return 0;
        seen.add(name);
        const own = gzipSync(Buffer.from(chunk.code)).length;
        return own + chunk.imports.reduce((sum, imported) => sum + walk(imported), 0);
      };
      const entries = Object.values(bundle).filter(
        (chunk) => chunk.type === "chunk" && chunk.isEntry,
      );
      for (const entry of entries) {
        const size = walk(entry.fileName);
        if (size > ENTRY_GZIP_BUDGET_BYTES) {
          this.error(
            `Entry chunk ${entry.fileName} loads ${size} bytes gzipped (with its static imports, ` +
              `${[...seen].join(", ")}), over the ${ENTRY_GZIP_BUDGET_BYTES} budget. ` +
              `Something that belongs behind React.lazy (a Workbench or Engine Tuning page) is ` +
              `imported statically from the Drive path.`,
          );
        }
      }
    },
  };
}

// In dev (`npm run dev`), Vite serves the UI on :5173 and proxies the
// backend paths to the FastAPI server on :8000. In production
// (`npm run build`), the SPA is built into ./dist and served by the
// backend itself (see scripts/serve_dashboard.py mounting).
export default defineConfig({
  plugins: [react(), tailwindcss(), entryBudget()],
  server: {
    port: 5173,
    proxy: {
      "/status": "http://127.0.0.1:8000",
      "/config": "http://127.0.0.1:8000",
      "/control": "http://127.0.0.1:8000",
      "/pair": "http://127.0.0.1:8000",
      "/anomalies": "http://127.0.0.1:8000",
      "/telemetry": "http://127.0.0.1:8000",
      // Without these, dev (`pnpm dev`) serves index.html for the API path,
      // useBindings/useBehaviorModels parse HTML as JSON and silently get
      // {} — so decoded gauges show *raw* bytes (e.g. ECM_RPM 16375 instead
      // of raw×0.125 ≈ 2047). Prod serves the SPA same-origin so it's moot
      // there; this only bites the dev server.
      "/bindings": "http://127.0.0.1:8000",
      "/models": "http://127.0.0.1:8000",
      "/obd": "http://127.0.0.1:8000",
      "/dids": "http://127.0.0.1:8000",
      "/test-profiles": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    // The Drive screen ships in the entry chunk; the Workbench and each Engine
    // Tuning page are `React.lazy` chunks split by Rollup (R28.6). The entry's
    // gzip size is held to `ENTRY_GZIP_BUDGET_BYTES` by `entryBudget` above.
  },
  test: {
    environment: "jsdom",
    globals: true,
    // Unit/component tests live under src/. The Playwright e2e specs
    // (e2e/*.spec.ts) use @playwright/test, not vitest — keep them out of
    // this runner or it tries to execute them and fails on the foreign API.
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    setupFiles: ["./src/test-setup.ts"],
    // Don't load assets / css through the test harness — they're not
    // exercised in unit tests and pulling them in slows the suite.
    css: false,
  },
});

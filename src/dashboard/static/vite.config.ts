/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// In dev (`npm run dev`), Vite serves the UI on :5173 and proxies the
// backend paths to the FastAPI server on :8000. In production
// (`npm run build`), the SPA is built into ./dist and served by the
// backend itself (see scripts/serve_dashboard.py mounting).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/status": "http://127.0.0.1:8000",
      "/config": "http://127.0.0.1:8000",
      "/control": "http://127.0.0.1:8000",
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
    // Single chunk for the dashboard — page-load cost is minimal and Pi
    // bandwidth is rarely the bottleneck. Revisit if the bundle grows
    // past a few hundred KB.
    rollupOptions: { output: { manualChunks: undefined } },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    // Don't load assets / css through the test harness — they're not
    // exercised in unit tests and pulling them in slows the suite.
    css: false,
  },
});

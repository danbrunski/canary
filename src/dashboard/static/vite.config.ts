import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev (`npm run dev`), Vite serves the UI on :5173 and proxies the
// backend paths to the FastAPI server on :8000. In production
// (`npm run build`), the SPA is built into ./dist and served by the
// backend itself (see scripts/serve_dashboard.py mounting).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/status": "http://127.0.0.1:8000",
      "/config": "http://127.0.0.1:8000",
      "/anomalies": "http://127.0.0.1:8000",
      "/telemetry": "http://127.0.0.1:8000",
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
});

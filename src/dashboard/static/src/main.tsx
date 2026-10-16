import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import "./index.css";
import { consumeTokenHandoff } from "./lib/tokenHandoff";

const root = document.getElementById("root");
if (!root) {
  throw new Error("missing #root in index.html");
}
// Desk launcher handoff: store `#...?token=` and scrub it before any hook reads the hash.
consumeTokenHandoff();
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// Offline shell caching (Task 6.12). Production only — in `pnpm dev` a SW
// would cache Vite's HMR chunks and fight the dev server. The SW itself
// never caches dynamic API/WS paths (see public/sw.js).
if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* SW unsupported / blocked — the app still works fully online */
    });
  });
}

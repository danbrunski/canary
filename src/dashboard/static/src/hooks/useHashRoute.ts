import { useCallback, useEffect, useState } from "react";

import { DEFAULT_ITEM, resolveItem } from "../nav";
import type { AppMode, WorkbenchItem } from "../types";

interface Route {
  mode: AppMode;
  item: WorkbenchItem;
}

// #drive            → driving
// #wb               → workbench, default item
// #wb/<item>        → workbench, that item (validated)
function parseHash(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "");
  if (raw === "" || raw === "drive") {
    return { mode: "driving", item: DEFAULT_ITEM };
  }
  const [head, tail] = raw.split("/", 2);
  if (head === "wb") {
    const item = (tail && resolveItem(tail)) || DEFAULT_ITEM;
    return { mode: "parked", item };
  }
  return { mode: "driving", item: DEFAULT_ITEM };
}

function toHash(r: Route): string {
  return r.mode === "driving" ? "#drive" : `#wb/${r.item}`;
}

/** Mirror the Drive↔Workbench context + active Workbench view in
 *  `location.hash` so a head-unit / phone reload (kiosk) lands back on the
 *  same screen, and so a view is shareable/bookmarkable. Our own writes use
 *  `replaceState` (no history spam); external changes / back-forward come
 *  back through `hashchange`. */
export function useHashRoute(): {
  mode: AppMode;
  item: WorkbenchItem;
  setMode: (m: AppMode) => void;
  setItem: (i: WorkbenchItem) => void;
} {
  const [route, setRoute] = useState<Route>(() =>
    parseHash(typeof window === "undefined" ? "" : window.location.hash),
  );

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    const want = toHash(route);
    if (window.location.hash !== want) {
      window.history.replaceState(null, "", want);
    }
  }, [route]);

  const setMode = useCallback((mode: AppMode) => setRoute((r) => ({ ...r, mode })), []);
  const setItem = useCallback((item: WorkbenchItem) => setRoute((r) => ({ ...r, item })), []);

  return { mode: route.mode, item: route.item, setMode, setItem };
}

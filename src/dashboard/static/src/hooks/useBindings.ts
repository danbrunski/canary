import { useEffect, useState } from "react";

import { getBindings } from "../api/http";
import type { BindingsByChannel } from "../types";

/** Load `/bindings` once at app start.
 *
 *  Bindings rarely change during a live session — a hand-edit to
 *  `data/bindings.yaml` triggers it, but those happen between RE
 *  sessions, not during a drive. One fetch on mount is plenty; the
 *  dashboard can be reloaded if the file changed.
 *
 *  Returns ``{}`` while loading or if the fetch failed — the panel
 *  rendering path falls back to "no scale known" cleanly. */
export function useBindings() {
  const [bindings, setBindings] = useState<BindingsByChannel>({});

  useEffect(() => {
    const controller = new AbortController();
    getBindings(controller.signal)
      .then((data) => setBindings(data))
      .catch(() => {
        /* fall back to empty; panels handle the no-binding case */
      });
    return () => controller.abort();
  }, []);

  return bindings;
}

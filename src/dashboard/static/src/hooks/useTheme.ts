import { useCallback, useEffect, useState } from "react";

import { readString, writeString } from "../util/storage";

export type Theme = "dark" | "night";

const STORAGE_KEY = "canary.theme";

function readStored(): Theme {
  return readString(STORAGE_KEY) === "night" ? "night" : "dark";
}

/** Day(dark) / night(dimmer) theme. `dark` is the default and leaves the
 *  `<html>` attribute unset so the base `@theme` palette applies verbatim;
 *  `night` sets `data-theme="night"`, which `index.css` overrides to a
 *  lower-glare palette. Persisted so a kiosk reload keeps the choice. */
export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(readStored);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "night") {
      root.dataset.theme = "night";
    } else {
      delete root.dataset.theme;
    }
    writeString(STORAGE_KEY, theme);
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((t) => (t === "dark" ? "night" : "dark"));
  }, []);

  return { theme, toggle };
}

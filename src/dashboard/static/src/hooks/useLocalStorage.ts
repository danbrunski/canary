import { useEffect, useState } from "react";

import { readJson, writeJson } from "../util/storage";

/** `useState` that mirrors a JSON-serializable value to localStorage:
 *  read once on mount, written on every change (mount included, matching
 *  the hand-rolled `useEffect(..., [value])` panels this replaces). Storage
 *  failures are swallowed — the value still lives for the session. */
export function useLocalStorageJson<T>(key: string, initial: T) {
  const [value, setValue] = useState<T>(() => readJson(key, initial));
  useEffect(() => {
    writeJson(key, value);
  }, [key, value]);
  return [value, setValue] as const;
}

// localStorage accessors. The try/catch (private mode / quota / disabled)
// plus the "empty string → removeItem" rule were copy-pasted into
// useControlToken, useCompanionSettings, useTheme and four panels with the
// same `/* storage disabled */` comment. One definition; callers keep their
// own normalization (trim, slash-strip, tri-state) on top.

/** Stored raw string, or `fallback` when absent/unavailable. */
export function readString(key: string, fallback = ""): string {
  try {
    return window.localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

/** Write a raw string; an empty value removes the key. Failures are
 *  swallowed — the in-memory value still applies for the session. */
export function writeString(key: string, value: string): void {
  try {
    if (value) {
      window.localStorage.setItem(key, value);
    } else {
      window.localStorage.removeItem(key);
    }
  } catch {
    /* storage unavailable — keep the in-memory value */
  }
}

/** Parse a JSON-stored value, or `fallback` when absent/unparseable. */
export function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

/** JSON-serialize and store. Failures are swallowed. */
export function writeJson(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable — keep the in-memory value */
  }
}

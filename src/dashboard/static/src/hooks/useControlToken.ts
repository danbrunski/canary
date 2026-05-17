import { useState } from "react";

const STORAGE_KEY = "canary.controlToken";

function readStoredToken(): string {
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function writeStoredToken(token: string): void {
  try {
    if (token) {
      window.localStorage.setItem(STORAGE_KEY, token);
    } else {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    /* storage may be disabled; keep the in-memory value */
  }
}

export function useControlToken() {
  const [token, setTokenState] = useState(readStoredToken);

  const setToken = (next: string) => {
    const clean = next.trim();
    setTokenState(clean);
    writeStoredToken(clean);
  };

  return {
    token,
    setToken,
    hasToken: token.length > 0,
  };
}

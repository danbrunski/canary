import { useState } from "react";

const BASE_KEY = "canary.companion.baseUrl";
const TOKEN_KEY = "canary.companion.token";
const DEFAULT_BASE = "http://127.0.0.1:8765";

function readStored(key: string, fallback = ""): string {
  try {
    return window.localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function writeStored(key: string, value: string): void {
  try {
    if (value) {
      window.localStorage.setItem(key, value);
    } else {
      window.localStorage.removeItem(key);
    }
  } catch {
    /* storage may be disabled; keep the in-memory value */
  }
}

export interface CompanionSettings {
  baseUrl: string;
  token: string;
  setBaseUrl: (next: string) => void;
  setToken: (next: string) => void;
}

export function useCompanionSettings(): CompanionSettings {
  const [baseUrl, setBaseUrlState] = useState(() => readStored(BASE_KEY, DEFAULT_BASE));
  const [token, setTokenState] = useState(() => readStored(TOKEN_KEY));

  const setBaseUrl = (next: string) => {
    const clean = next.trim().replace(/\/+$/, "");
    setBaseUrlState(clean);
    writeStored(BASE_KEY, clean);
  };

  const setToken = (next: string) => {
    const clean = next.trim();
    setTokenState(clean);
    writeStored(TOKEN_KEY, clean);
  };

  return { baseUrl, token, setBaseUrl, setToken };
}

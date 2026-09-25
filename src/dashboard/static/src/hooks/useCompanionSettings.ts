import { useState } from "react";

import { readString, writeString } from "../util/storage";

const BASE_KEY = "canary.companion.baseUrl";
const TOKEN_KEY = "canary.companion.token";
const DEFAULT_BASE = "http://127.0.0.1:8765";

export interface CompanionSettings {
  baseUrl: string;
  token: string;
  setBaseUrl: (next: string) => void;
  setToken: (next: string) => void;
}

export function useCompanionSettings(): CompanionSettings {
  const [baseUrl, setBaseUrlState] = useState(() => readString(BASE_KEY, DEFAULT_BASE));
  const [token, setTokenState] = useState(() => readString(TOKEN_KEY));

  const setBaseUrl = (next: string) => {
    const clean = next.trim().replace(/\/+$/, "");
    setBaseUrlState(clean);
    writeString(BASE_KEY, clean);
  };

  const setToken = (next: string) => {
    const clean = next.trim();
    setTokenState(clean);
    writeString(TOKEN_KEY, clean);
  };

  return { baseUrl, token, setBaseUrl, setToken };
}

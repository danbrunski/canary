import { useCallback, useMemo, useState } from "react";

import { readString, writeString } from "../util/storage";

const BASE_KEY = "canary.companion.baseUrl";
const TOKEN_KEY = "canary.companion.token";
/** Blank means the companion is this server: the laptop daemon serves the
 *  companion routes same-origin, so relative paths reach it. */
const DEFAULT_BASE = "";
/** The default before companion mode moved into the daemon: the standalone
 *  companion API on port 8765, deleted. A browser that saved it would send
 *  every companion request to nothing, so it reads as blank. */
const LEGACY_STANDALONE_BASE = "http://127.0.0.1:8765";

function readBase(): string {
  const stored = readString(BASE_KEY, DEFAULT_BASE);
  return stored === LEGACY_STANDALONE_BASE ? DEFAULT_BASE : stored;
}

export interface CompanionSettings {
  baseUrl: string;
  token: string;
  setBaseUrl: (next: string) => void;
  setToken: (next: string) => void;
}

/** Where companion requests go and which token they carry. With no base URL
 *  (the default) the companion is this server, so requests are relative and
 *  the dashboard's control token is the one that authorizes them; a remote
 *  companion keeps its own base URL and token. */
export function companionAccess(
  companion: Pick<CompanionSettings, "baseUrl" | "token">,
  controlToken: string,
): { baseUrl: string; token: string; sameOrigin: boolean } {
  const baseUrl = companion.baseUrl.trim();
  if (!baseUrl) return { baseUrl: "", token: controlToken, sameOrigin: true };
  return { baseUrl, token: companion.token, sameOrigin: false };
}

export function useCompanionSettings(): CompanionSettings {
  const [baseUrl, setBaseUrlState] = useState(readBase);
  const [token, setTokenState] = useState(() => readString(TOKEN_KEY));

  const setBaseUrl = useCallback((next: string) => {
    const clean = next.trim().replace(/\/+$/, "");
    setBaseUrlState(clean);
    writeString(BASE_KEY, clean);
  }, []);

  const setToken = useCallback((next: string) => {
    const clean = next.trim();
    setTokenState(clean);
    writeString(TOKEN_KEY, clean);
  }, []);

  // The same object until a field changes: a memoised page that takes it as a
  // prop would otherwise re-render with every render of the app.
  return useMemo(
    () => ({ baseUrl, token, setBaseUrl, setToken }),
    [baseUrl, token, setBaseUrl, setToken],
  );
}

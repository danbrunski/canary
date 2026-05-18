import { useState } from "react";

import { readString, writeString } from "../util/storage";

const STORAGE_KEY = "canary.controlToken";

export function useControlToken() {
  const [token, setTokenState] = useState(() => readString(STORAGE_KEY));

  const setToken = (next: string) => {
    const clean = next.trim();
    setTokenState(clean);
    writeString(STORAGE_KEY, clean);
  };

  return {
    token,
    setToken,
    hasToken: token.length > 0,
  };
}

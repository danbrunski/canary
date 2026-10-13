import { useState } from "react";

import { readString, writeString } from "../util/storage";

export const CONTROL_TOKEN_KEY = "canary.controlToken";

export function useControlToken() {
  const [token, setTokenState] = useState(() => readString(CONTROL_TOKEN_KEY));

  const setToken = (next: string) => {
    const clean = next.trim();
    setTokenState(clean);
    writeString(CONTROL_TOKEN_KEY, clean);
  };

  return {
    token,
    setToken,
    hasToken: token.length > 0,
  };
}

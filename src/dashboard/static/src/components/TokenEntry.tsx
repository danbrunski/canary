import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

interface TokenEntryValue {
  /** True from a `requestTokenEntry()` until the header gate picks it up. */
  pending: boolean;
  /** Ask the header gate to open and focus its field (and, on a phone, the
   *  menu that holds it). Safe to call from any panel. */
  requestTokenEntry: () => void;
  /** The gate calls this once it has opened for the request. */
  consume: () => void;
}

const NOOP: TokenEntryValue = { pending: false, requestTokenEntry: () => {}, consume: () => {} };

const TokenEntryContext = createContext<TokenEntryValue>(NOOP);

/** Wraps the shell so any panel can open the one control-token entry point
 *  (the header `TokenGate`) without prop-drilling. Outside a provider the
 *  hook is a no-op, so a panel rendered alone in a test needs no setup. */
export function TokenEntryProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState(false);
  const requestTokenEntry = useCallback(() => setPending(true), []);
  const consume = useCallback(() => setPending(false), []);
  const value = useMemo(
    () => ({ pending, requestTokenEntry, consume }),
    [pending, requestTokenEntry, consume],
  );
  return <TokenEntryContext.Provider value={value}>{children}</TokenEntryContext.Provider>;
}

export function useTokenEntry(): TokenEntryValue {
  return useContext(TokenEntryContext);
}

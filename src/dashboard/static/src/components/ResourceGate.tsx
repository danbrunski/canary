import type { ReactNode } from "react";

import { cn, placeholderClass } from "../styles";
import { TokenRequired } from "./TokenRequired";

interface ResourceGateProps<T> {
  /** False shows `enabledMessage` instead of fetching/rendering — the
   *  "enter the control token first" case every gated panel has. Defaults
   *  to true for panels with nothing to gate on (e.g. unauthenticated
   *  reads). */
  enabled?: boolean;
  /** What the control token unlocks, as a verb phrase ("view tire
   *  wear"). Renders the shared `TokenRequired` placeholder. */
  tokenPurpose?: string;
  /** Overrides the placeholder entirely; prefer `tokenPurpose`. */
  enabledMessage?: ReactNode;
  loading: boolean;
  loadingMessage?: ReactNode;
  error: string | null;
  data: T | null;
  children: (data: T) => ReactNode;
}

/** The token/loading/error 3-state every `useControlResource` panel
 *  re-implemented as its own trio of early returns (Task 3.3). Renders its
 *  placeholder text with the same `placeholderClass` those panels already
 *  used, so existing role/text-based tests keep passing. */
export function ResourceGate<T>({
  enabled = true,
  tokenPurpose = "continue",
  enabledMessage,
  loading,
  loadingMessage = "loading…",
  error,
  data,
  children,
}: ResourceGateProps<T>) {
  if (!enabled) {
    return enabledMessage === undefined ? (
      <TokenRequired purpose={tokenPurpose} />
    ) : (
      <p className={placeholderClass}>{enabledMessage}</p>
    );
  }
  if (loading) return <p className={placeholderClass}>{loadingMessage}</p>;
  if (error || !data) {
    return <p className={cn(placeholderClass, "text-bad")}>{error ?? "no data"}</p>;
  }
  return <>{children(data)}</>;
}

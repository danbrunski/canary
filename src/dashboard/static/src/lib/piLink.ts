import type { PiLink } from "../types";

/** The host part of a base URL, or the whole string when it does not parse. */
function hostOf(baseUrl: string): string {
  try {
    return new URL(baseUrl).host;
  } catch {
    return baseUrl;
  }
}

/** What to call a linked Pi: its own hostname when the companion has probed
 *  it, otherwise the host of the URL it is linked at. */
export function piDisplayName(pi: PiLink): string {
  return pi.identity?.hostname || hostOf(pi.base_url);
}

/** How the link came about, in words ("found on the network", not the
 *  internal `discovered`). */
export const PI_SOURCE_LABEL: Record<PiLink["source"], string> = {
  discovered: "found on the network",
  selected: "chosen here",
  configured: "from config",
};

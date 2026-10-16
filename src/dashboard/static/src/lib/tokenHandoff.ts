// Token handoff for the desk launcher (E08.9, scripts/canary_desk.py).
//
// The launcher opens  http://127.0.0.1:<port>/#wb/<item>?token=<value>
// so a local operator never pastes the control token. A query inside the
// fragment is never sent to the server, so the token cannot reach a request
// line or the daemon log. The value is percent-encoded
// (`urllib.parse.quote(token, safe="")`); `+` is literal, not a space.
//
// Call `consumeTokenHandoff()` in main.tsx BEFORE the React tree renders, so
// no hook ever sees the raw hash: the token is moved into the same
// localStorage slot `useControlToken` reads, and the address bar is rewritten
// to the bare route (`#wb/<item>`).

import { CONTROL_TOKEN_KEY } from "../hooks/useControlToken";
import { writeString } from "../util/storage";

function decode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value; // malformed escape: keep the raw text rather than drop it
  }
}

/** Move a `?token=` out of `location.hash` into storage and scrub the URL.
 *  A hash without a `token` key is left untouched. A present-but-empty token
 *  stores nothing (the existing one survives) but the URL is still scrubbed,
 *  since the leftover `?token=` would break the route parse. Returns true
 *  when a non-empty token was stored. */
export function consumeTokenHandoff(): boolean {
  const hash = window.location.hash;
  const q = hash.indexOf("?");
  if (q < 0) return false;

  const route = hash.slice(0, q); // "#wb/<item>", or "#" for "#?token=x"
  let found = false;
  let token = "";
  for (const pair of hash.slice(q + 1).split("&")) {
    const eq = pair.indexOf("=");
    const key = eq < 0 ? pair : pair.slice(0, eq);
    if (decode(key) !== "token") continue;
    found = true;
    token = eq < 0 ? "" : decode(pair.slice(eq + 1)).trim();
    break;
  }
  if (!found) return false;

  if (token) writeString(CONTROL_TOKEN_KEY, token);

  const { pathname, search } = window.location;
  window.history.replaceState(null, "", `${pathname}${search}${route === "#" ? "" : route}`);
  return token.length > 0;
}

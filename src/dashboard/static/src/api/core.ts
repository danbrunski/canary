export class HttpError extends Error {
  status: number;
  body: string;

  constructor(status: number, body: string) {
    super(`HTTP ${status}: ${body.slice(0, 120)}`);
    this.status = status;
    this.body = body;
  }
}

/** A request that never got a response at all — the daemon is unreachable
 *  (Pi powered down, off-network, `canary.local` not resolving), as opposed
 *  to an error the daemon actually returned.
 *
 *  Worth distinguishing loudly: `public/sw.js` serves the app shell from
 *  cache on a failed navigation, so an unreachable Pi presents as a
 *  dashboard that opens and looks healthy until the first button is
 *  pressed. Browsers name only the transport there ("NetworkError when
 *  attempting to fetch resource"), which reads as an app bug rather than
 *  "the truck's hotspot dropped". */
export class NetworkError extends Error {
  url: string;

  constructor(url: string, cause?: unknown) {
    super(`Network request to ${url} failed`, { cause });
    this.name = "NetworkError";
    this.url = url;
  }
}

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

/** An aborted `fetch` rejects with a `DOMException`, which does not reliably
 *  sit on `Error`'s prototype chain (it doesn't under jsdom), so narrow on
 *  the name structurally rather than via `instanceof`. Getting this wrong
 *  reports every cancelled poll as a dead daemon. */
function isAbort(err: unknown): boolean {
  return (
    typeof err === "object" && err !== null && (err as { name?: unknown }).name === "AbortError"
  );
}

/** `fetch`, with transport failures raised as `NetworkError`. Caller-driven
 *  cancellation is passed through untouched — `AbortError` is not a
 *  connectivity problem, and callers switch on `err.name`. */
export async function fetchOrNetworkError(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  try {
    return await fetch(input, init);
  } catch (err) {
    if (isAbort(err)) throw err;
    throw new NetworkError(requestUrl(input), err);
  }
}

export function apiUrl(path: string, baseUrl?: string): string {
  const base = baseUrl?.trim().replace(/\/+$/, "") ?? "";
  if (!base) return path;
  return `${base}${path.startsWith("/") ? path : `/${path}`}`;
}

export async function fetchJson<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const response = await fetchOrNetworkError(input, init);
  const text = await response.text();
  if (!response.ok) {
    throw new HttpError(response.status, text);
  }
  if (!text) {
    return {} as T;
  }
  return JSON.parse(text) as T;
}

export async function controlJson<T>(
  path: string,
  token: string,
  init: RequestInit = {},
  baseUrl?: string,
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const body = init.body;
  if (body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return fetchJson<T>(apiUrl(path, baseUrl), { ...init, headers });
}

/** An authorized request whose response body is a file (a zip, a GPX track),
 *  not JSON. Same auth header and error shape as `controlJson` (R08.10). */
export async function controlBlob(
  path: string,
  token: string,
  init: RequestInit = {},
  baseUrl?: string,
): Promise<Blob> {
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetchOrNetworkError(apiUrl(path, baseUrl), { ...init, headers });
  if (!response.ok) {
    throw new HttpError(response.status, await response.text());
  }
  return response.blob();
}

export function jsonBody(value: unknown): string {
  return JSON.stringify(value);
}

export type QueryValue = string | number | boolean | null | undefined;

/** `?a=1&b=x` from a params object, skipping null, undefined and "" — so an
 *  unset optional never reaches the server as `key=` or `key=null`. Returns
 *  "" when nothing is set. Key order is the object's. */
export function queryString(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export class HttpError extends Error {
  status: number;
  body: string;

  constructor(status: number, body: string) {
    super(`HTTP ${status}: ${body.slice(0, 120)}`);
    this.status = status;
    this.body = body;
  }
}

export function apiUrl(path: string, baseUrl?: string): string {
  const base = baseUrl?.trim().replace(/\/+$/, "") ?? "";
  if (!base) return path;
  return `${base}${path.startsWith("/") ? path : `/${path}`}`;
}

export async function fetchJson<T>(
  input: RequestInfo,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(input, init);
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

export function jsonBody(value: unknown): string {
  return JSON.stringify(value);
}

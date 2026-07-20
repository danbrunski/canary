/** Base64-encode a File's bytes, chunked to stay under the call-stack limit
 *  `String.fromCharCode(...bytes)` hits on large files (model artifacts). */
export async function fileToBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  const chunkSize = 8192;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.slice(index, index + chunkSize));
  }
  return btoa(binary);
}

/** This page's own origin when it's being served *from* the Pi (not
 *  localhost/dev) — the companion API needs this to know which host to
 *  pull a session from during a sync-to-laptop. */
export function currentPiHost(): string | null {
  const host = window.location.hostname;
  if (host === "localhost" || host === "127.0.0.1" || host === "::1") {
    return null;
  }
  return window.location.origin;
}

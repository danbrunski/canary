/**
 * phone-ui P2.1: the install manifest a phone reads, checked against the files
 * that actually ship. A manifest icon that 404s is silently dropped by the
 * browser, and one missing `public/sw.js` shell entry fails the whole service
 * worker install (`cache.addAll` is all-or-nothing) — neither shows anywhere
 * else.
 */
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

// vitest's root is the dashboard directory.
const root = process.cwd();
const publicDir = resolve(root, "public");

interface ManifestIcon {
  src: string;
  type: string;
  sizes: string;
  purpose?: string;
}

interface Manifest {
  display: string;
  orientation: string;
  theme_color: string;
  background_color: string;
  icons: ManifestIcon[];
}

const manifest = JSON.parse(
  readFileSync(resolve(publicDir, "manifest.webmanifest"), "utf8"),
) as Manifest;

/** Width and height from a PNG's IHDR chunk. */
function pngSize(path: string): [number, number] {
  const bytes = readFileSync(path);
  expect(bytes.subarray(1, 4).toString("ascii")).toBe("PNG");
  return [bytes.readUInt32BE(16), bytes.readUInt32BE(20)];
}

function publicFile(src: string): string {
  return resolve(publicDir, src.replace(/^\//, ""));
}

describe("web app manifest", () => {
  it("does not lock the orientation and keeps the status bar", () => {
    expect(manifest.orientation).toBe("any");
    expect(manifest.display).toBe("standalone");
    expect(manifest.theme_color).toBe("#101418");
  });

  it("lists only icons that exist, at the sizes they claim", () => {
    for (const icon of manifest.icons) {
      const path = publicFile(icon.src);
      expect(existsSync(path), icon.src).toBe(true);
      if (icon.type === "image/png") {
        const [w, h] = pngSize(path);
        expect(`${w}x${h}`, icon.src).toBe(icon.sizes);
      }
    }
  });

  it("has the 192 and 512 px PNGs and a maskable icon Android asks for", () => {
    const png = manifest.icons.filter((i) => i.type === "image/png");
    const any = png.filter((i) => (i.purpose ?? "any").split(" ").includes("any"));
    expect(any.map((i) => i.sizes).sort()).toEqual(["192x192", "512x512"]);
    expect(png.some((i) => i.purpose?.split(" ").includes("maskable"))).toBe(true);
  });

  it("links an apple-touch-icon that exists", () => {
    const html = readFileSync(resolve(root, "index.html"), "utf8");
    const href = /<link rel="apple-touch-icon" href="([^"]+)"/.exec(html)?.[1];
    expect(href).toBeDefined();
    expect(existsSync(publicFile(href ?? ""))).toBe(true);
  });

  it("caches only shell files that exist", () => {
    const sw = readFileSync(resolve(publicDir, "sw.js"), "utf8");
    const block = /const SHELL = \[([^\]]*)\]/.exec(sw)?.[1] ?? "";
    const entries = [...block.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
    expect(entries).toContain("/manifest.webmanifest");
    for (const icon of manifest.icons) expect(entries).toContain(icon.src);
    for (const entry of entries) {
      if (entry === "/" || entry === "/index.html") continue; // built by vite
      expect(existsSync(publicFile(entry)), entry).toBe(true);
    }
  });
});

import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Auto-unmount any rendered tree between tests so a stray hook in test A
// doesn't carry state into test B.
afterEach(() => {
  cleanup();
});

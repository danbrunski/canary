// ESLint for the dashboard (R08.13, operator 2026-09-24).
//
// Type-aware on purpose: the promise rules (no-floating-promises,
// no-misused-promises) and the hook rules target the fetch-in-effect bugs the
// 2026-09-24 refactor review found. Formatting is Prettier's job;
// eslint-config-prettier switches off every rule that would fight it.
//
// Every rule is an error. Violations that existed when this landed are
// recorded per file and rule in eslint-suppressions.json (ESLint bulk
// suppressions), so a new one fails the gate. When you fix some, run
// `pnpm lint:prune`: an unused suppression also fails `pnpm lint`, so the
// file can only shrink. Never re-run --suppress-all to make a new violation
// go away; fix it.

import js from "@eslint/js";
import prettier from "eslint-config-prettier";
import { defineConfig, globalIgnores } from "eslint/config";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default defineConfig(
  globalIgnores(["dist", "test-results", "playwright-report", "blob-report", "coverage"]),
  js.configs.recommended,
  tseslint.configs.recommendedTypeChecked,
  reactHooks.configs.flat["recommended-latest"],
  {
    languageOptions: {
      globals: globals.browser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // React ignores an event handler's return value, so an async onClick is
      // not a misused promise. Async callbacks handed to timers, listeners and
      // other functions still are, and stay checked.
      "@typescript-eslint/no-misused-promises": [
        "error",
        { checksVoidReturn: { attributes: false } },
      ],
      // The preset makes these warnings; warnings do not fail the gate and
      // cannot be bulk-suppressed, so they are errors here.
      "react-hooks/exhaustive-deps": "error",
      "react-hooks/incompatible-library": "error",
      "react-hooks/unsupported-syntax": "error",
    },
  },
  {
    // vitest mocks are `any` by construction, and mock implementations are
    // async without awaiting. These rules only produce noise in tests.
    files: ["**/*.test.ts", "**/*.test.tsx", "src/test/**"],
    rules: {
      "@typescript-eslint/no-unsafe-argument": "off",
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
      "@typescript-eslint/require-await": "off",
      "@typescript-eslint/unbound-method": "off",
    },
  },
  {
    // The Playwright suite and its config live in tsconfig.e2e.json, which the
    // app's solution tsconfig does not reference.
    files: ["e2e/**/*.ts", "playwright.config.ts"],
    languageOptions: {
      globals: globals.node,
      parserOptions: { projectService: false, project: ["./tsconfig.e2e.json"] },
    },
  },
  {
    files: ["**/*.js"],
    extends: [tseslint.configs.disableTypeChecked],
    languageOptions: { globals: globals.node },
  },
  prettier,
);

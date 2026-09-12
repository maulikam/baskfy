import { existsSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * AUDIT 0.10 — without these files, thrown fetches and `notFound()` paint Next's bare pages
 * with no nav. A missing file is the regression; existence is the acceptance criterion.
 */
const APP = resolve(process.cwd(), "src/app");

const REQUIRED = [
  "(app)/error.tsx",
  "(app)/not-found.tsx",
  "(app)/loading.tsx",
  "(marketing)/error.tsx",
  "(marketing)/not-found.tsx",
  "(marketing)/loading.tsx",
  "global-error.tsx",
] as const;

describe("shelled error / not-found / loading boundaries", () => {
  for (const relative of REQUIRED) {
    it(`ships ${relative}`, () => {
      expect(existsSync(resolve(APP, relative))).toBe(true);
    });
  }
});

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * AUDIT 4.4 — React `cache()` around RSC loaders so generateMetadata + page do not double-fetch.
 */
const LIB = resolve(process.cwd(), "src/lib");

describe("RSC loaders wrap in React cache()", () => {
  for (const relative of ["explore/fetch.ts", "basket/fetch.ts", "market/fetch.ts"] as const) {
    it(`${relative} uses cache(`, () => {
      const source = readFileSync(resolve(LIB, relative), "utf8");
      expect(source).toMatch(/from "react"/);
      expect(source).toMatch(/cache\(/);
    });
  }
});

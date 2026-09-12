/**
 * apps/web must ignore local env files so AUTH_SECRET / BASKFY_JWT_SECRET cannot be staged
 * from this package directory (AUDIT 2.8).
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

describe("apps/web/.gitignore", () => {
  const source = readFileSync(resolve(process.cwd(), ".gitignore"), "utf8");

  it("ignores .env and .env.* (keeps .env.example)", () => {
    expect(source).toMatch(/^\.env$/m);
    expect(source).toMatch(/^\.env\.\*$/m);
    expect(source).toMatch(/^!\.env\.example$/m);
  });
});

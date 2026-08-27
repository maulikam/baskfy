/**
 * Where a sign-in with no `?next=` lands.
 *
 * It has now moved twice: `/build` originally, `/home` when SC9 gave the product a landing
 * surface, and back to `/build` on 27 Aug 2026 at Maulik's instruction — `/home` assumes a user
 * who already holds something, and today's do not, so it lands them on an empty page instead of
 * the tool they came for.
 *
 * Moving it is one line in `actions/auth.ts`. What makes it worth a test is the second half:
 * seventeen Playwright waits across eleven specs assert the landing, and a mismatch there fails as
 * a thirty-second timeout in CI rather than as anything that names the cause. So this asserts both
 * halves together, and a future change to one without the other fails here, in a second, with a
 * sentence.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const WEB_ROOT = resolve(process.cwd());
const ACTIONS = readFileSync(resolve(WEB_ROOT, "src/app/actions/auth.ts"), "utf8");

function specFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((entry) => {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) return specFiles(path);
    return path.endsWith(".ts") ? [path] : [];
  });
}

const SPECS = specFiles(resolve(WEB_ROOT, "e2e")).map((path) => ({
  path: relative(WEB_ROOT, path),
  source: readFileSync(path, "utf8"),
}));

/** The destination, read out of the source rather than imported: the module is `"use server"`. */
function declaredDestination(): string {
  const match = /const DEFAULT_DESTINATION = "([^"]+)"/.exec(ACTIONS);
  return match?.[1] ?? "";
}

describe("signing in lands on the product's landing surface", () => {
  it("defaults to /build", () => {
    // Maulik, 27 Aug 2026: "after login make sure we land into build only". `/home` answers
    // "what do I hold" for someone who holds something; a new account does not, and the first
    // screen after signing in should be the one they came to use.
    expect(declaredDestination()).toBe("/build");
  });

  it("declares exactly one default", () => {
    expect(ACTIONS.match(/const DEFAULT_DESTINATION/g)).toHaveLength(1);
  });
});

describe("the end-to-end suite waits for the same place", () => {
  it("no spec still waits for /home after signing in", () => {
    // The stale assertion from the previous destination. Left behind, it fails as a timeout in
    // CI with nothing in the message about why.
    const offenders = SPECS.filter((spec) => /waitForURL\(\/\\\/home/.test(spec.source)).map(
      (spec) => spec.path,
    );
    expect(offenders).toEqual([]);
  });

  it("the specs wait for the destination the action actually declares", () => {
    // Read from the source rather than hard-coded, so this test cannot drift from the constant
    // it exists to protect: move `DEFAULT_DESTINATION` again and this still holds the specs to it.
    const destination = declaredDestination().replace(/^\//, "");
    const pattern = new RegExp(`waitForURL\\(/\\\\/${destination}`);
    const waiting = SPECS.filter((spec) => pattern.test(spec.source));
    expect(waiting.length).toBeGreaterThan(0);
  });

  it("keeps the deliberate waits on a screen under /build", () => {
    // Rewriting these too would have been the easy over-correction: `/build/<id>` is where a saved
    // screen lives, and those assertions are about navigation, not about signing in.
    const deep = SPECS.filter((spec) => /waitForURL\(\/\\\/build\\\//.test(spec.source));
    expect(deep.length).toBeGreaterThan(0);
  });
});

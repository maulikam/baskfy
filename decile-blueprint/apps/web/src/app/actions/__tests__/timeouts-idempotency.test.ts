import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * AUDIT 4.5 — timeouts on mutations and the browser client; no timeout on DELETE /me;
 * Idempotency-Key on POST /portfolio and checkout.
 */
const ROOT = resolve(process.cwd(), "src");

function read(relative: string): string {
  return readFileSync(resolve(ROOT, relative), "utf8");
}

describe("action and client timeouts / idempotency", () => {
  it("startCheckout sends Idempotency-Key and AbortSignal.timeout", () => {
    const source = read("app/actions/billing.ts");
    expect(source).toMatch(/Idempotency-Key/);
    expect(source).toMatch(/AbortSignal\.timeout/);
  });

  it("POST /portfolio sends Idempotency-Key and AbortSignal.timeout", () => {
    const source = read("lib/portfolio/create.ts");
    expect(source).toMatch(/Idempotency-Key/);
    expect(source).toMatch(/\/api\/v1\/portfolio[\s\S]*AbortSignal\.timeout/s);
  });

  it("browserApi installs a timed fetch", () => {
    const source = read("lib/api/browser.ts");
    expect(source).toMatch(/fetch:\s*browserTimedFetch/);
    expect(source).toMatch(/AbortSignal\.timeout/);
  });

  it("deleteAccount DELETE /me has no AbortSignal", () => {
    const source = read("app/actions/account.ts");
    const deleteBlock = source.slice(source.indexOf("export async function deleteAccount"));
    expect(deleteBlock).toMatch(/method:\s*"DELETE"/);
    expect(deleteBlock).not.toMatch(/AbortSignal\.timeout/);
  });

  it("broker connect and sync carry AbortSignal.timeout", () => {
    const source = read("lib/brokers/fetch.ts");
    expect(source.match(/AbortSignal\.timeout/g)?.length ?? 0).toBeGreaterThanOrEqual(2);
  });
});

/**
 * The Content-Security-Policy header is well-formed.
 *
 * The spec, not the current string: a policy may name each directive **once**. A repeated
 * directive is not a hard failure — the browser honours the first occurrence and drops the rest —
 * which is exactly why it survived unnoticed. What it costs is a
 * "Ignoring duplicate Content-Security-Policy directive" warning in the console on every page
 * load of the authenticated app, and a developer who learns to scroll past CSP warnings will
 * scroll past the one that matters.
 */
import { describe, expect, it } from "vitest";

import { contentSecurityPolicy, staticContentSecurityPolicy } from "@/middleware";

/** Directive names in the order they appear, `default-src 'self'` → `default-src`. */
function directiveNames(policy: string): string[] {
  return policy
    .split(";")
    .map((directive) => directive.trim())
    .filter(Boolean)
    .map((directive) => directive.split(/\s+/)[0] ?? "");
}

function duplicates(names: readonly string[]): string[] {
  const seen = new Set<string>();
  const repeated = new Set<string>();
  for (const name of names) {
    if (seen.has(name)) repeated.add(name);
    seen.add(name);
  }
  return [...repeated];
}

const POLICIES: ReadonlyArray<readonly [string, (isDev: boolean) => string]> = [
  ["nonce policy", (isDev) => contentSecurityPolicy("deadbeef", isDev)],
  ["static public policy", (isDev) => staticContentSecurityPolicy(isDev)],
];

describe("the Content-Security-Policy header", () => {
  for (const [label, build] of POLICIES) {
    for (const isDev of [true, false]) {
      const mode = isDev ? "development" : "production";

      it(`names every directive exactly once — ${label}, ${mode}`, () => {
        const names = directiveNames(build(isDev));
        expect(duplicates(names), `repeated directives in the ${label}`).toEqual([]);
      });

      it(`still carries the directives docs/11 pins — ${label}, ${mode}`, () => {
        const policy = build(isDev);
        expect(policy).toContain("default-src 'self'");
        expect(policy).toContain("object-src 'none'");
        expect(policy).toContain("base-uri 'self'");
        // Spec: Kite Publisher hand-off must be an allowed form target (AUDIT 0.2 / 2.3).
        // The old pin of exactly `form-action 'self'` locked in the bug that blocked Invest.
        expect(policy).toContain("form-action 'self' https://kite.zerodha.com");
        expect(policy).toContain("frame-ancestors 'none'");
      });
    }
  }

  it("keeps the per-request nonce in script-src", () => {
    expect(contentSecurityPolicy("abc123", false)).toContain("'nonce-abc123'");
  });

  it("allows eval only in development", () => {
    expect(contentSecurityPolicy("abc123", true)).toContain("'unsafe-eval'");
    expect(contentSecurityPolicy("abc123", false)).not.toContain("'unsafe-eval'");
    expect(staticContentSecurityPolicy(true)).toContain("'unsafe-eval'");
    expect(staticContentSecurityPolicy(false)).not.toContain("'unsafe-eval'");
  });
});

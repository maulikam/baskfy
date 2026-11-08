/**
 * The split-horizon rule for the API origin, enforced by a scan.
 *
 * `docs/08` §3 runs `web` and `api` in one compose network behind Caddy, and the staging host is
 * gated with basic auth. That makes "where is the API" two different answers:
 *
 * - **From the server process** — `http://api:8000`, straight across the container network.
 * - **From a browser** — `https://staging.baskfy.com`, through Caddy, past the gate the human
 *   already authenticated to.
 *
 * `serverApiOrigin()` answers the first, `apiOrigin()` the second. Both are one import away from
 * each other and neither fails loudly when misused, which is exactly the shape of bug that ships:
 * a server component fetching the public origin gets a 401 from the gate and renders its error
 * state; a client component emitting the internal origin into an `href` produces a link that goes
 * nowhere, only in production, only in the container.
 *
 * So the rule is scanned rather than remembered, the same way `no-jargon.test.ts` scans labels and
 * `no-any.test.ts` scans types. The house rule these serve is #2 — this asserts the spec ("server
 * fetches use the internal origin; anything a browser follows uses the public one"), not what the
 * code currently happens to do.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { apiOrigin, serverApiOrigin } from "@/lib/api/config";

const ROOT = resolve(process.cwd(), "src");

function sourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((entry) => {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) return entry === "__tests__" ? [] : sourceFiles(path);
    if (!/\.tsx?$/.test(path)) return [];
    return /\.test\.tsx?$/.test(path) ? [] : [path];
  });
}

const FILES = sourceFiles(ROOT).map((path) => ({
  path: relative(ROOT, path),
  source: readFileSync(path, "utf8"),
}));

/** A file is a client module when the directive is in its opening lines, where Next requires it. */
function isClientModule(source: string): boolean {
  return /^\s*(?:\/\*[\s\S]*?\*\/\s*)?["']use client["']/m.test(
    source.split("\n").slice(0, 6).join("\n"),
  );
}

/**
 * Files whose `apiOrigin()` result is consumed by a browser, so the public origin is correct even
 * though the module itself renders on the server. Each needs a reason, and the list is the
 * sanctioned escape hatch — the same convention `no-jargon.test.ts`'s `ALLOWED` uses.
 */
const BROWSER_CONSUMERS: Record<string, string> = {
  "middleware.ts": "builds the CSP's connect-src, which the browser enforces",
  "components/billing/invoice-table.tsx": "renders an <a href> the reader clicks",
  "lib/api/config.ts": "defines both functions",
};

describe("the two origins do not get mixed up", () => {
  it("no client module reaches for the server-side origin", () => {
    // The hydration trap: SSR would emit http://api:8000 and the browser would emit the public
    // origin, so the markup and the client disagree about a URL nobody can reach.
    const offenders = FILES.filter(
      (file) => isClientModule(file.source) && file.source.includes("serverApiOrigin"),
    ).map((file) => file.path);
    expect(offenders).toEqual([]);
  });

  it("every server module that fetches the API uses the server-side origin", () => {
    const offenders = FILES.filter((file) => {
      if (isClientModule(file.source)) return false;
      if (file.path in BROWSER_CONSUMERS) return false;
      // `fetch(`${apiOrigin()}` …` — the public origin used as a fetch target on the server.
      return /fetch\(\s*`\$\{apiOrigin\(\)\}/.test(file.source);
    }).map((file) => file.path);
    expect(offenders).toEqual([]);
  });

  it("keeps a reason on file for every module allowed to keep the public origin", () => {
    for (const [path, reason] of Object.entries(BROWSER_CONSUMERS)) {
      expect(FILES.some((file) => file.path === path)).toBe(true);
      expect(reason.length).toBeGreaterThan(20);
    }
  });
});

describe("serverApiOrigin falls back rather than inventing a host", () => {
  const KEY = "BASKFY_INTERNAL_API_ORIGIN";

  it("is exactly the public origin when the internal one is unset", () => {
    // Local development, `pnpm test`, and `next dev` all take this path, so the split must be
    // invisible outside the container.
    delete process.env[KEY];
    expect(serverApiOrigin()).toBe(apiOrigin());
  });

  it("is the public origin for a blank value, not an empty string", () => {
    // A compose file with `BASKFY_INTERNAL_API_ORIGIN=` sets it to "". Treating that as a real
    // origin would produce `fetch("/api/v1/...")` with no host and a confusing runtime error.
    process.env[KEY] = "   ";
    expect(serverApiOrigin()).toBe(apiOrigin());
    delete process.env[KEY];
  });

  it("uses the internal origin when one is configured", () => {
    process.env[KEY] = "http://api:8000";
    expect(serverApiOrigin()).toBe("http://api:8000");
    delete process.env[KEY];
  });

  it("strips a trailing slash and a duplicated /api/v1, exactly as apiOrigin does", () => {
    // The two functions read env vars that people copy between each other, so they have to
    // tolerate the same mistakes. `/api/v1/api/v1/...` is the bug apiOrigin's comment names.
    process.env[KEY] = "http://api:8000/";
    expect(serverApiOrigin()).toBe("http://api:8000");
    process.env[KEY] = "http://api:8000/api/v1";
    expect(serverApiOrigin()).toBe("http://api:8000");
    delete process.env[KEY];
  });
});

describe("apiOrigin is the single public resolver", () => {
  const KEY = "NEXT_PUBLIC_API_URL";

  /*
   * `vi.stubEnv` rather than `Object.defineProperty(process.env, …)`. Node's `process.env` is an
   * exotic object that accepts only a configurable, writable *and* enumerable data descriptor,
   * and the descriptor these two cases used named the first of the three — so both threw a
   * `TypeError` before reaching their assertion. Vitest special-cases `NODE_ENV` in `stubEnv`
   * and `unstubAllEnvs` puts it back, which is the same reason `hsts-preload.test.ts` uses it.
   * The spec asserted is unchanged: production must be told the origin, development may assume.
   */
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("throws in production when NEXT_PUBLIC_API_URL is unset (no localhost fallback)", () => {
    vi.stubEnv(KEY, undefined);
    vi.stubEnv("NODE_ENV", "production");
    expect(() => apiOrigin()).toThrow(/NEXT_PUBLIC_API_URL must be set in production/);
  });

  it("falls back to localhost only outside production", () => {
    vi.stubEnv(KEY, undefined);
    vi.stubEnv("NODE_ENV", "development");
    expect(apiOrigin()).toBe("http://localhost:8000");
  });

  it("never reads the retired NEXT_PUBLIC_API_ORIGIN env", () => {
    // The dual-resolver bug: middleware used to honour _ORIGIN while config ignored it.
    const source = readFileSync(resolve(process.cwd(), "src/middleware.ts"), "utf8");
    const configSource = readFileSync(resolve(process.cwd(), "src/lib/api/config.ts"), "utf8");
    expect(source).not.toMatch(/process\.env\.NEXT_PUBLIC_API_ORIGIN/);
    expect(configSource).not.toMatch(/process\.env\.NEXT_PUBLIC_API_ORIGIN/);
  });
});

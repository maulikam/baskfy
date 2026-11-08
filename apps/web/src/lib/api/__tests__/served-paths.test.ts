/**
 * Every API path this app hands to a fetch helper must be a path the API actually serves.
 *
 * ## The bug this exists to prevent
 *
 * M48 (`6195b57`, 27 Aug 2026) renamed the page tree `app/(app)/baskets/` → `app/(app)/discover/`.
 * That was a deliberate product decision — `lib/nav.ts` still carries the `/baskets → /discover`
 * redirects. What was *not* a decision is that the same rename swept three **API** path constants
 * along with it:
 *
 * | site | became | should have stayed |
 * |---|---|---|
 * | `lib/basket/fetch.ts` `fetchBasket` | `/discover` | `/baskets` |
 * | `lib/basket/fetch.ts` `fetchLatestPlan` | `/discover/plan` | `/baskets/plan` |
 * | `lib/create/fetch.ts` `createPrivateBasket` | `/api/v1/cb/discover` | `/api/v1/cb/baskets` |
 *
 * The API never moved: `routers/baskets.py` and `routers/curated_create.py` serve those three
 * routes today and have since M22 and SC8. So `/discover/featured`, `/discover/plan` and the save
 * button on `/create` had been talking to 404s for two weeks.
 *
 * Nothing caught it, and nothing *could* have. These paths are strings handed to a hand-rolled
 * `fetch` wrapper, so the generated client's types never see them; the wrappers turn any non-OK
 * response into a domain error (`BasketUnavailable`, `CreateBasketError`) and the pages render
 * that as a tidy empty state. A 404 and "the database is empty" are the same pixels. The only
 * thing louder than the bug was the page insisting everything was fine.
 *
 * ## What this scans
 *
 * Two shapes, which between them are every un-typed API call in the app:
 *
 * 1. **Full literals** — anything containing `/api/v1`, whether that is a generated-client call
 *    (`api.GET("/api/v1/screens")`, already type-checked, scanned anyway because it costs nothing)
 *    or a hand-built URL (`fetch(\`${'$'}{serverApiOrigin()}/api/v1/checkout/session\`)`).
 * 2. **Wrapper arguments** — the ~19 modules that define a private `readJson(path)` /
 *    `readOrNull(path)` / `post(path)` helper around `` `${'$'}{serverApiOrigin()}/api/v1${'$'}{path}` ``
 *    and then call it with a bare string. This is where all three of M48's casualties lived, and
 *    it is the only shape with no type behind it at all.
 *
 * House rule #2: this asserts the spec — "a path this app fetches is a path the API serves" —
 * not current behaviour. `KNOWN_UNSERVED` below is the register of paths that break the spec
 * today; it is empty, and it is checked in **both** directions, so an entry cannot outlive its
 * bug and cannot be used to make the suite green.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const WEB_ROOT = resolve(process.cwd());
const SRC = join(WEB_ROOT, "src");
const OPENAPI = resolve(WEB_ROOT, "../../packages/api-client/openapi.json");

/** docs/07: "Base: `/api/v1`". */
const API_PREFIX = "/api/v1";

/**
 * Paths the app fetches that the API does not serve — the register, not an excuse.
 *
 * An entry is a **known open bug with an owner**, not a permission. Both directions are asserted:
 * a path missing from here fails `no unserved path is fetched`, and an entry that has *stopped*
 * being a bug fails `the register has no stale entries`, so whoever lands the route deletes the
 * line in the same commit. Adding to this list to make the suite green is the one thing it must
 * never be used for.
 */
const KNOWN_UNSERVED: ReadonlyMap<string, string> = new Map([
  // Empty, and that is the finding, not an omission: as of 12 Sep 2026 every path this app
  // fetches is a path the API serves.
  //
  // It was written with one entry — `/twt/backtest`, `lib/twt/fetch.ts` `fetchBacktest`, the
  // third casualty the audit turned up and the one out of this agent's lane — and the route
  // landed the same afternoon. `keeps no stale entries` went red within minutes and the entry was
  // deleted, which is the mechanism working: a register that cannot outlive its bug.
]);

/* ------------------------------------------------------------------ file walk */

function sources(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "__tests__" || entry === "node_modules" || entry === "test") continue;
      sources(full, out);
      continue;
    }
    if (!/\.tsx?$/.test(entry)) continue;
    if (/\.(test|spec)\.tsx?$/.test(entry)) continue;
    out.push(full);
  }
  return out;
}

/* ------------------------------------------------------------------ comment strip */

/**
 * Blank out comments, leaving every string and template literal in place.
 *
 * Character-by-character rather than by regex because half the prose in this codebase is a
 * docstring naming a route, and `http://` inside a string is not the start of a comment. Replaced
 * with spaces rather than removed so line numbers survive for the failure message.
 */
function stripComments(src: string): string {
  const out: string[] = [];
  let i = 0;
  let state: "code" | "line" | "block" | "'" | '"' | "`" = "code";
  while (i < src.length) {
    const c = src.charAt(i);
    const next = src.charAt(i + 1);
    if (state === "code") {
      if (c === "/" && next === "/") { state = "line"; out.push("  "); i += 2; continue; }
      if (c === "/" && next === "*") { state = "block"; out.push("  "); i += 2; continue; }
      if (c === "'" || c === '"' || c === "`") state = c;
      out.push(c);
      i += 1;
      continue;
    }
    if (state === "line") {
      if (c === "\n") { state = "code"; out.push(c); } else out.push(" ");
      i += 1;
      continue;
    }
    if (state === "block") {
      if (c === "*" && next === "/") { state = "code"; out.push("  "); i += 2; continue; }
      out.push(c === "\n" ? c : " ");
      i += 1;
      continue;
    }
    // inside a string or template literal
    if (c === "\\") { out.push(c, next); i += 2; continue; }
    if (c === state) state = "code";
    out.push(c);
    i += 1;
  }
  return out.join("");
}

/* ------------------------------------------------------------------ extraction */

interface Site {
  readonly file: string;
  readonly line: number;
  readonly path: string;
  readonly how: string;
}

const lineOf = (src: string, index: number): number =>
  src.slice(0, index).split("\n").length;

/** Path characters we accept inside a literal, including `${...}` interpolations. */
const PATH_CHARS = String.raw`[A-Za-z0-9_\-./{}$()]`;

function fullLiterals(file: string, src: string): Site[] {
  const found: Site[] = [];
  const re = new RegExp(String.raw`/api/v1(/${PATH_CHARS}*)`, "g");
  for (const m of src.matchAll(re)) {
    found.push({ file, line: lineOf(src, m.index), path: m[1] ?? "", how: "literal" });
  }
  return found;
}

/**
 * Names of the module's own path-taking fetch wrappers.
 *
 * Seeded with whatever function interpolates `` /api/v1${'$'}{ident} ``, then closed over
 * delegation: `readOrNull(path)` calling `readJson(path)` is just as much a wrapper as `readJson`,
 * and `lib/twt/fetch.ts` is built exactly that way.
 */
function wrapperNames(src: string): Set<string> {
  const lines = src.split("\n");
  const declaredAt = (lineIndex: number): string | null => {
    for (let j = lineIndex - 1; j >= 0; j -= 1) {
      const text = lines[j] ?? "";
      const m = /^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*[(<]/.exec(text);
      if (m?.[1]) return m[1];
      const c = /^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?(?:\(|function\b)/.exec(text);
      if (c?.[1]) return c[1];
    }
    return null;
  };

  const names = new Set<string>();
  lines.forEach((line, i) => {
    if (!/\/api\/v1\$\{\w+\}/.test(line)) return;
    const name = declaredAt(i);
    if (name) names.add(name);
  });

  // Delegation: f(...) { ... known(<bareIdent>...) ... } is itself a wrapper.
  for (let pass = 0; pass < 4; pass += 1) {
    const before = names.size;
    lines.forEach((line, i) => {
      for (const known of names) {
        const call = new RegExp(String.raw`\b${known}\s*(?:<[^()<>]*>)?\s*\(\s*\w+\s*[,)]`);
        if (!call.test(line)) continue;
        const name = declaredAt(i + 1);
        if (name && name !== known) names.add(name);
      }
    });
    if (names.size === before) break;
  }
  return names;
}

function wrapperCalls(file: string, src: string): Site[] {
  const names = wrapperNames(src);
  if (names.size === 0) return [];
  const found: Site[] = [];
  for (const name of names) {
    const re = new RegExp(
      String.raw`\b${name}\s*(?:<[^()<>]*>)?\s*\(\s*["'\`](/${PATH_CHARS}*)["'\`]`,
      "g",
    );
    for (const m of src.matchAll(re)) {
      found.push({ file, line: lineOf(src, m.index), path: m[1] ?? "", how: `wrapper:${name}()` });
    }
  }
  return found;
}

/* ------------------------------------------------------------------ matching */

/** `${anything}` → `{}`; drop query and fragment; strip a trailing slash. */
function normalise(path: string): string {
  let p = path.replace(/\$\{[^}]*\}/g, "{}");
  p = (p.split("?")[0] ?? p).split("#")[0] ?? p;
  p = p.replace(/\/+$/, "");
  return p === "" ? "/" : p;
}

function segmentMatches(scanned: string, served: string): boolean {
  if (served.startsWith("{") && served.endsWith("}")) return true;
  if (scanned === "{}") return true;
  const glued = scanned.indexOf("{}");
  // `/explore${toQuery(params)}` is a query suffix welded to the last segment, not a new one.
  if (glued > 0) return served.startsWith(scanned.slice(0, glued));
  return scanned === served;
}

function isServed(path: string, served: readonly string[][]): boolean {
  const scanned = normalise(path).split("/").filter(Boolean);
  return served.some(
    (candidate) =>
      candidate.length === scanned.length &&
      candidate.every((seg, i) => segmentMatches(scanned[i] ?? "", seg)),
  );
}

/* ------------------------------------------------------------------ the tests */

const spec = JSON.parse(readFileSync(OPENAPI, "utf8")) as { paths: Record<string, unknown> };
const servedPaths: string[] = Object.keys(spec.paths)
  .filter((p) => p.startsWith(API_PREFIX))
  .map((p) => p.slice(API_PREFIX.length));
const servedSegments = servedPaths.map((p) => p.split("/").filter(Boolean));

const sites: Site[] = sources(SRC).flatMap((file) => {
  const src = stripComments(readFileSync(file, "utf8"));
  const rel = relative(WEB_ROOT, file);
  return [...fullLiterals(rel, src), ...wrapperCalls(rel, src)];
});

const unserved = sites.filter((site) => !isServed(site.path, servedSegments));

describe("every API path the web app fetches is a path the API serves", () => {
  it("finds the API calls at all — a scan that sees nothing proves nothing", () => {
    // Guards the scanner itself. If a refactor changes how calls are written, this drops to zero
    // and the suite would go green by seeing nothing, which is the failure mode of every
    // source-scanning test. 150 is comfortably under the ~190 sites present when this was written.
    expect(sites.length).toBeGreaterThan(150);
    expect(sites.some((s) => s.path === "/baskets")).toBe(true);
    expect(sites.some((s) => s.path.startsWith("/api/v1/") || s.path.startsWith("/"))).toBe(true);
  });

  it("serves every path that is fetched", () => {
    const unregistered = unserved.filter(
      (site) => !KNOWN_UNSERVED.has(normalise(site.path.replace(/^\/api\/v1/, ""))),
    );
    const report = unregistered.map(
      (s) => `${s.file}:${s.line} fetches ${s.path} via ${s.how} — openapi.json has no such route`,
    );
    expect(report).toEqual([]);
  });

  it("keeps no stale entries in the register", () => {
    const fixed = [...KNOWN_UNSERVED.keys()].filter((path) => isServed(path, servedSegments));
    expect(
      fixed.map(
        (p) => `${p} is served now — delete its KNOWN_UNSERVED entry in the commit that landed it`,
      ),
    ).toEqual([]);
  });

  it("keeps no entry for a path nothing fetches any more", () => {
    const orphans = [...KNOWN_UNSERVED.keys()].filter(
      (path) => !sites.some((s) => normalise(s.path.replace(/^\/api\/v1/, "")) === path),
    );
    expect(
      orphans.map((p) => `nothing fetches ${p} any more — delete its KNOWN_UNSERVED entry`),
    ).toEqual([]);
  });
});

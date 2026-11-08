#!/usr/bin/env node
/**
 * Every route that `next.config.ts` redirects away must not still hold a real page.
 *
 * Next evaluates `redirects()` *before* the filesystem routes, so a page file at a redirected
 * path is unreachable — it compiles, ships and is never rendered. Tree 6 moved the whole consumer
 * IA (`/dashboard` → `/market/today`, `/screens` → `/build`, …) and left a redirect stub at each
 * old path as a second line of defence for a client-side navigation that never reaches the edge.
 *
 * **That rationale was measured and does not hold.** Every redirected path returns 308 from the
 * edge on a plain GET *and* on `RSC: 1` + `Next-Router-Prefetch: 1` — the request the App Router
 * client makes on a soft navigation — and `next.config.ts` sets no `output:` mode, so
 * `redirects()` always runs. The stubs were unreachable, which makes them dead code that cannot
 * be exercised or tested. They were deleted (25 Aug 2026); this script now asserts they stay gone.
 *
 * So the rule is: **a redirected path must hold no page file at all.** A page there is content
 * someone will keep editing without ever being able to see it — which is exactly what happened
 * to `/investments/[id]`, three *real* pages shadowed by `/investments/:path*` and silently
 * skipped by the wildcard hole this script used to have.
 *
 * **What this script must never be read as licence to delete.** "Unreachable" is not the same as
 * "unfinished". A page that resolves but is deliberately thin — waiting on a dependency, holding
 * a route open so detail tabs and nav links already work — is *load-bearing*, and deleting it
 * breaks navigation that exists today. `/basket/[slug]/constituents` is the standing example: an
 * SC5 stub whose rebalance timeline and holdings distribution land when SC3 publishes immutable
 * constituent versions. It sits at no redirected path, renders real content, and this check
 * correctly ignores it. The test is the redirect table, never how finished a page looks.
 */
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const WEB_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const CONFIG = join(WEB_DIR, "next.config.ts");
const APP_DIR = join(WEB_DIR, "src", "app", "(app)");

/** `{ source: "/dashboard", destination: "/market/today", permanent: true }` → the source paths. */
function redirectSources() {
  const config = readFileSync(CONFIG, "utf8");
  const sources = [];
  for (const match of config.matchAll(/\{\s*source:\s*"([^"]+)"[^}]*\}/g)) {
    const source = match[1];
    if (source) sources.push(source);
  }
  return sources;
}

/** `/screens/:id/columns` → `src/app/(app)/screens/[id]/columns/page.tsx`. */
function pageFileFor(source) {
  const segments = source
    .split("/")
    .filter(Boolean)
    .map((segment) => (segment.startsWith(":") ? `[${segment.slice(1).replace(/\*$/, "")}]` : segment));
  return join(APP_DIR, ...segments, "page.tsx");
}

/** The directory a wildcard source shadows: `/investments/:path*` → `.../(app)/investments`. */
function wildcardDirFor(source) {
  if (!source.includes("*")) return null;
  const segments = source.split("/").filter(Boolean);
  segments.pop(); // drop the `:path*` segment itself
  return segments.length ? join(APP_DIR, ...segments) : null;
}

/** Every `page.tsx` at or beneath `dir`, relative to the web root. */
function pagesUnder(dir) {
  const found = [];
  const walk = (current) => {
    if (!existsSync(current)) return;
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const next = join(current, entry.name);
      if (entry.isDirectory()) walk(next);
      else if (entry.name === "page.tsx") found.push(next);
    }
  };
  walk(dir);
  return found;
}

const offenders = [];

for (const source of redirectSources()) {
  // The exact path a source shadows.
  const file = pageFileFor(source);
  if (existsSync(file)) {
    offenders.push(`${source} -> ${file.replace(`${WEB_DIR}/`, "")}`);
  }

  // A wildcard source shadows everything beneath it. This used to `return null` and skip the
  // source entirely, which is how three real pages under `/investments/:path*` stayed invisible.
  const dir = wildcardDirFor(source);
  if (dir) {
    for (const shadowed of pagesUnder(dir)) {
      offenders.push(`${source} -> ${shadowed.replace(`${WEB_DIR}/`, "")}`);
    }
  }
}

if (offenders.length) {
  console.error("page files at paths that next.config.ts redirects away:");
  for (const offender of offenders) console.error(`  ${offender}`);
  console.error(
    "\nNone of these can ever render: Next runs redirects() before the filesystem routes, on\n" +
      "plain GETs and on RSC prefetches alike. Move the content to the destination path, or\n" +
      "delete the file. A redirect stub is not an exception — it is unreachable too.",
  );
  process.exit(1);
}

console.log(`ok — ${redirectSources().length} redirected source(s), no page file shadowed by any of them`);

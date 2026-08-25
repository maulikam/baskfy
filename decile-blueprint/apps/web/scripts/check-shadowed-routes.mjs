#!/usr/bin/env node
/**
 * Every route that `next.config.ts` redirects away must not still hold a real page.
 *
 * Next evaluates `redirects()` *before* the filesystem routes, so a page file at a redirected
 * path is unreachable — it compiles, ships and is never rendered. Tree 6 moved the whole consumer
 * IA (`/dashboard` → `/market/today`, `/screens` → `/build`, …) and left a redirect stub at each
 * old path deliberately, as a second line of defence for a client-side navigation that never
 * reaches the edge. A stub is fine. A *page* is not: it is content someone will keep editing
 * without ever being able to see it.
 *
 * So the rule this asserts is not "no file at a redirected path" but "nothing but a redirect at a
 * redirected path".
 */
import { readFileSync, existsSync } from "node:fs";
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

/** `/screens/:id/columns` → `src/app/(app)/screens/[id]/columns/page.tsx`. Null when unmappable. */
function pageFileFor(source) {
  if (source.includes(":path*") || source.includes("*")) return null;
  const segments = source
    .split("/")
    .filter(Boolean)
    .map((segment) => (segment.startsWith(":") ? `[${segment.slice(1)}]` : segment));
  return join(APP_DIR, ...segments, "page.tsx");
}

/** A file is a stub when it renders nothing and only calls `redirect`. */
function isRedirectStub(file) {
  const body = readFileSync(file, "utf8");
  if (!/\bredirect\(/.test(body)) return false;
  // Any JSX means it is rendering something a reader will never see.
  return !/<[A-Za-z]/.test(body);
}

const offenders = [];
let checked = 0;
for (const source of redirectSources()) {
  const file = pageFileFor(source);
  if (!file || !existsSync(file)) continue;
  checked += 1;
  if (!isRedirectStub(file)) {
    offenders.push(`${source} -> ${file.replace(`${WEB_DIR}/`, "")}`);
  }
}

if (offenders.length) {
  console.error("routes that redirect away but still hold a real page:");
  for (const offender of offenders) console.error(`  ${offender}`);
  console.error(
    "\nA page at a redirected path never renders: Next runs redirects() before the filesystem\n" +
      "routes. Move the content to the new path, or reduce the file to a redirect stub.",
  );
  process.exit(1);
}

console.log(`ok — ${checked} redirected route(s) hold a redirect stub and nothing else`);

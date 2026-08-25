/**
 * The client-JS budget — Prompt 16 deliverable 5.
 *
 *   docs/11 §"Performance budgets": "Client: JS on the screens route < 250 KB gzip after
 *   code-splitting the table and charts."
 *
 * Reads `.next/app-build-manifest.json`, which is Next's own record of exactly which chunks a
 * route loads, gzips each one at the level a CDN would, and sums. That is the number a browser
 * pays on first load — not the raw byte count, and not the whole `.next/static` directory.
 *
 *   node scripts/bundle-budget.mjs            # measure and print every route
 *   node scripts/bundle-budget.mjs --check    # ...and exit 1 if a budgeted route is over
 *
 * The measurement is also written to `benchmarks/results/screens_bundle.json` so that
 * `python -m benchmarks.report` can put it in the same table as the server-side budgets. That
 * file is the only reason this script knows about a directory outside `apps/web`.
 *
 * Why not `@next/bundle-analyzer`: it renders a treemap for a human to look at, which is a
 * different job from failing a build. docs/02 locks no bundle tooling and this needs none —
 * `zlib.gzipSync` is in Node.
 */

import { gzipSync } from "node:zlib";
import { readFileSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const WEB_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const REPO_ROOT = resolve(WEB_DIR, "..", "..");
const NEXT_DIR = join(WEB_DIR, process.env.BASKFY_WEB_DIST_DIR ?? ".next");
const MANIFEST = join(NEXT_DIR, "app-build-manifest.json");

/**
 * docs/11 names one route. The others are here so a regression on a page nobody budgeted is still
 * visible in the output — only the entries with a `limitKb` can fail the check.
 *
 * @type {Array<{route: string, label: string, limitKb: number | null, resultKey?: string}>}
 */
const BUDGETS = [
  {
    route: "/(app)/screens/page",
    label: "screens",
    limitKb: 250,
    resultKey: "screens_bundle",
  },
  { route: "/(app)/screens/[id]/page", label: "screens/[id]", limitKb: 250 },
  { route: "/(app)/dashboard/page", label: "dashboard", limitKb: null },
  { route: "/(app)/instruments/[symbol]/page", label: "instruments/[symbol]", limitKb: null },
  { route: "/(app)/market-health/page", label: "market-health", limitKb: null },
  { route: "/(app)/backtests/page", label: "backtests", limitKb: null },
];

function loadManifest() {
  if (!existsSync(MANIFEST)) {
    console.error(
      `${MANIFEST} does not exist. Run \`pnpm --filter @baskfy/web run build\` first.`,
    );
    process.exit(2);
  }
  return JSON.parse(readFileSync(MANIFEST, "utf8"));
}

/** Gzipped size of one built asset, in bytes. */
function gzippedBytes(assetPath) {
  const full = join(NEXT_DIR, assetPath);
  if (!existsSync(full)) return 0;
  // Level 9: what a CDN serving pre-compressed static assets stores. Level 6 (the default) would
  // report a few percent higher and make the budget look tighter than it is in production.
  return gzipSync(readFileSync(full), { level: 9 }).byteLength;
}

/**
 * Every JS chunk a route loads on first paint, de-duplicated.
 *
 * The manifest lists shared chunks once per route that uses them; a browser downloads each one
 * once, so summing the raw list would double-count the framework chunk against itself.
 */
function routeChunks(manifest, route) {
  const listed = manifest.pages?.[route];
  if (!listed) return null;
  return [...new Set(listed)].filter((asset) => asset.endsWith(".js"));
}

function measure(manifest, budget) {
  const chunks = routeChunks(manifest, budget.route);
  if (chunks === null) return { ...budget, missing: true, kb: 0, chunks: 0 };
  const bytes = chunks.reduce((total, asset) => total + gzippedBytes(asset), 0);
  return { ...budget, missing: false, kb: bytes / 1024, chunks: chunks.length };
}

function recordMeasurement(key, kb) {
  const dir = join(REPO_ROOT, "benchmarks", "results");
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, `${key}.json`),
    `${JSON.stringify(
      {
        key,
        value: Number(kb.toFixed(1)),
        unit: "KB",
        method: "sum of gzip(level 9) over the route's first-load JS in app-build-manifest.json",
        dataset: "a production `next build` of apps/web",
      },
      null,
      2,
    )}\n`,
    "utf8",
  );
}

function main() {
  const check = process.argv.includes("--check");
  const manifest = loadManifest();
  const measured = BUDGETS.map((budget) => measure(manifest, budget));

  console.log("Route                        First-load JS (gzip)   Budget    Chunks");
  const failures = [];
  const unknown = [];
  for (const row of measured) {
    if (row.missing) {
      unknown.push(row.route);
      console.log(`${row.label.padEnd(28)} ${"— not in manifest —".padEnd(22)}`);
      continue;
    }
    const size = `${row.kb.toFixed(1)} KB`;
    const limit = row.limitKb === null ? "—" : `${row.limitKb} KB`;
    const over = row.limitKb !== null && row.kb > row.limitKb;
    console.log(
      `${row.label.padEnd(28)} ${size.padEnd(22)} ${limit.padEnd(9)} ${String(row.chunks)}` +
        (over ? "   OVER BUDGET" : ""),
    );
    if (over) failures.push(`${row.label}: ${size} exceeds ${limit}`);
    if (row.resultKey) recordMeasurement(row.resultKey, row.kb);
  }

  if (unknown.length > 0) {
    // A budgeted route that vanished from the manifest is a renamed file, not a passing budget.
    console.error(`\nThese budgeted routes are not in the manifest: ${unknown.join(", ")}`);
    if (check) process.exit(1);
  }
  if (failures.length > 0) {
    console.error(`\n${failures.length} route(s) over budget:`);
    for (const failure of failures) console.error(`  ${failure}`);
    console.error(
      "\ndocs/11: \"JS on the screens route < 250 KB gzip after code-splitting the table and " +
        'charts." Split with next/dynamic before raising this number.',
    );
    if (check) process.exit(1);
  }
}

main();

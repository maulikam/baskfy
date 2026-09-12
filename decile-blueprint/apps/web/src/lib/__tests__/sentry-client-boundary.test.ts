import { readdirSync, readFileSync, existsSync } from "node:fs";
import { resolve, relative, sep } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * **Sentry is a server-side dependency of this app, and nothing enforced that until now.**
 *
 * `src/instrumentation.ts` states the architecture in prose: "This file runs on the server. There
 * is deliberately **no** `instrumentation-client.ts` and **no** `withSentryConfig` wrapper in
 * `next.config.ts`, which is what would pull the Sentry SDK into the browser bundle." The
 * consequence it accepts — browser exceptions are not reported — is recorded in
 * `docs/DECISIONS.md` §17.12, and the reason is the 250 KB gzip client budget in docs/11.
 *
 * That property was real but **unguarded**. It held because two files did not exist. Adding either
 * one is a three-line change that no test, lint rule or type error would have objected to, and the
 * cost would not have shown up as a failure — it would have shown up as ~40 KB of browser SDK in
 * every route's first load, a new set of network calls on every page, and a decision reversed by
 * accident rather than on purpose. House rule 2 asks tests to assert the spec; the spec is written
 * down in two places and had nothing holding it.
 *
 * **Why it was written on 12 Sep 2026.** Maulik reported an uncaught
 * `TypeError: Cannot read properties of undefined (reading 'startTime')` at `et.reportAllChanges`
 * in his browser console on the deployed app. `reportAllChanges` is a web-vitals name and
 * `@sentry/nextjs` bundles web-vitals, so Sentry's browser instrumentation was the obvious suspect.
 * It was not the culprit — the shipped client bundle contains no Sentry and no web-vitals at all
 * (`gates/sentry-vitals.md` has the build evidence), and `reportAllChanges` is never a callable in
 * any installed package, so it cannot produce that stack frame. The investigation's durable output
 * is not a fix to a bug we did not have; it is this file, which makes the "no Sentry in the
 * browser" answer checkable in a second instead of re-derived from a production build.
 *
 * These assertions are deliberately about *reachability*, not about a built artifact: a test that
 * greps `.next/` would need `next build` to run first, which is `scripts/bundle-budget.mjs`'s job
 * and is the same division of labour `code-splitting.test.ts` already draws.
 */
const APP_ROOT = process.cwd();
const SRC = resolve(APP_ROOT, "src");

/**
 * Next's client-instrumentation hook, under every name and location Next accepts it. Next looks
 * for this file at the project root *or* inside `src/`, and any of four extensions — so a test
 * that checked only one spelling would be a test that looks like it guards something.
 *
 * @see https://nextjs.org/docs/app/api-reference/file-conventions/instrumentation-client
 */
const CLIENT_INSTRUMENTATION_PATHS = ["", "src"].flatMap((dir) =>
  ["ts", "tsx", "js", "mjs"].map((ext) => resolve(APP_ROOT, dir, `instrumentation-client.${ext}`)),
);

/** Every `.ts`/`.tsx` module under `src/`, excluding test files. */
function sourceModules(dir: string = SRC): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = resolve(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "__tests__" || entry.name === "node_modules") continue;
      out.push(...sourceModules(path));
      continue;
    }
    if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(path);
  }
  return out;
}

/** `import ... from "@sentry/..."` / `require("@sentry/...")` — a *static* dependency edge. */
const STATIC_SENTRY_IMPORT =
  /(?:^|\n)\s*(?:import[^;\n]*from\s*|import\s*|(?:const|let|var)[^=\n]*=\s*require\s*\(\s*)["']@sentry\/[^"']*["']/;

/** `await import("@sentry/...")` — deferred, and therefore not in any bundle that never calls it. */
const DYNAMIC_SENTRY_IMPORT = /import\s*\(\s*["']@sentry\/[^"']*["']\s*\)/;

describe("Sentry stays out of the browser bundle", () => {
  it.each(CLIENT_INSTRUMENTATION_PATHS)(
    "%s does not exist — it is Next's hook for booting an SDK in every page",
    (path) => {
      expect(
        existsSync(path),
        `${relative(APP_ROOT, path)} exists. Next runs it in the browser on every route, which is ` +
          `precisely what src/instrumentation.ts and docs/DECISIONS.md §17.12 decided against. If ` +
          `that decision is being reversed, reverse it in those documents first and re-measure the ` +
          `docs/11 client budget with scripts/bundle-budget.mjs.`,
      ).toBe(false);
    },
  );

  it("next.config.ts is not wrapped in withSentryConfig", () => {
    const config = readFileSync(resolve(APP_ROOT, "next.config.ts"), "utf8");
    // The wrapper is the other door into the client bundle: it injects the browser SDK and the
    // source-map upload plugin regardless of what instrumentation files exist.
    expect(config).not.toMatch(/withSentryConfig/);
  });

  it("src/instrumentation.ts is the only module that reaches @sentry at all", () => {
    const importers = sourceModules()
      .filter((path) => /@sentry\//.test(readFileSync(path, "utf8")))
      .map((path) => relative(APP_ROOT, path));

    expect(importers).toEqual([`src${sep}instrumentation.ts`]);
  });

  it("instrumentation.ts imports @sentry dynamically, so the edge bundle never pulls it in", () => {
    const source = readFileSync(resolve(SRC, "instrumentation.ts"), "utf8");

    // A top-level `import ... from "@sentry/nextjs"` is evaluated by *every* runtime that loads
    // the module, including the edge runtime where the Node SDK does not belong. The file's own
    // comment says so; this is that comment with teeth.
    expect(source).not.toMatch(STATIC_SENTRY_IMPORT);
    expect(source).toMatch(DYNAMIC_SENTRY_IMPORT);
  });

  it("no client component imports @sentry", () => {
    // Belt and braces over the "only importer" assertion above: this is the one that keeps
    // reading true if someone later adds a legitimate *server* importer to the list.
    const offenders = sourceModules()
      .filter((path) => {
        const source = readFileSync(path, "utf8");
        return /^\s*["']use client["']/m.test(source) && /@sentry\//.test(source);
      })
      .map((path) => relative(APP_ROOT, path));

    expect(offenders).toEqual([]);
  });
});

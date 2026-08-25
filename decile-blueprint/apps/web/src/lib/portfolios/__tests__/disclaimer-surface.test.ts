import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { DISCLAIMER_TEXT } from "@/lib/disclaimer-text";

/**
 * House rule 9, second clause: **disclaimers are components, not footers.**
 *
 * The surfaces this leaf added — the portfolio tree, the per-broker roll-up, the units column —
 * are analytics surfaces, so docs/11 §Compliance requires the `<Disclaimer/>` on each. It is
 * already there, and *not* by anything in these files: `AppShell` mounts exactly one per app page,
 * which is what makes it impossible for a new page to ship without one.
 *
 * So the thing worth asserting is the pair of failures that are actually available here:
 *
 * 1. **A second `<Disclaimer/>`** inside one of these surfaces — the double-render tree 6 removed
 *    from the backtest detail page, and the reason `jargon-ban.test.ts` guards that file.
 * 2. **The sentence retyped as a footer string** somewhere in this tree, which is exactly the
 *    "footer, not component" shape the house rule forbids: a copy that can drift from the
 *    regulatory wording without the component changing at all.
 */

const WEB = resolve(process.cwd(), "src");
const SURFACES = [join(WEB, "app", "(app)", "portfolios"), join(WEB, "components", "portfolios")];

function sourceFiles(path: string): string[] {
  if (statSync(path).isFile()) return /\.tsx?$/.test(path) ? [path] : [];
  return readdirSync(path).flatMap((entry) => sourceFiles(join(path, entry)));
}

const FILES = SURFACES.flatMap(sourceFiles);
/** The two words the regulatory sentence opens with — enough to catch a retyped copy. */
const SENTENCE_HEAD = DISCLAIMER_TEXT.slice(0, 40);

describe("the new portfolio surfaces leave the Disclaimer to the app shell", () => {
  it("has files to scan", () => {
    expect(FILES.length).toBeGreaterThan(10);
  });

  it("mounts no second Disclaimer of its own", () => {
    const offenders = FILES.filter((file) => {
      const source = readFileSync(file, "utf8");
      return /import\s*\{[^}]*\bDisclaimer\b/.test(source) || /<Disclaimer\b/.test(source);
    }).map((file) => relative(process.cwd(), file));
    expect(offenders, offenders.join("\n")).toEqual([]);
  });

  it("never retypes the regulatory sentence as a footer string", () => {
    const offenders = FILES.filter((file) =>
      readFileSync(file, "utf8").includes(SENTENCE_HEAD),
    ).map((file) => relative(process.cwd(), file));
    expect(offenders, offenders.join("\n")).toEqual([]);
  });

  it("is covered because the app shell mounts the component for every app page", () => {
    const shell = readFileSync(join(WEB, "components", "shell", "app-shell.tsx"), "utf8");
    expect(shell).toMatch(/import \{ Disclaimer \}/);
    expect(shell).toMatch(/<Disclaimer\b/);
  });

  it("keeps every new surface inside that shell rather than beside it", () => {
    // A page under `(app)` renders through `(app)/layout.tsx`, which is what mounts `AppShell`.
    const layout = readFileSync(join(WEB, "app", "(app)", "layout.tsx"), "utf8");
    expect(layout).toMatch(/AppShell/);
    const pages = FILES.filter((file) => file.endsWith("page.tsx"));
    expect(pages.length).toBeGreaterThan(0);
    for (const page of pages) {
      expect(page, `${page} is outside the (app) group`).toContain(join("app", "(app)"));
    }
  });
});

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Prompt 8's fifth acceptance criterion: **"Zero `any` in apps/web."**
 *
 * ESLint enforces it while linting (`@typescript-eslint/no-explicit-any` and the five
 * `no-unsafe-*` rules), but a rule can be disabled inline and a file can be added to `ignores`.
 * This scans the source instead — the same approach `packages/core/tests/test_no_escape_hatches.py`
 * takes for the Python side, and for the same reason: a house rule that is only enforced by a tool
 * you can switch off is a house rule with an off switch.
 *
 * It also catches the near-neighbours, because `any` renamed is still `any`: a `@ts-ignore`, a
 * `@ts-expect-error`, or an `eslint-disable` of one of the rules above.
 */
/** Everything shipped or run by this package: application source, and the end-to-end tests. */
const ROOTS = [resolve(process.cwd(), "src"), resolve(process.cwd(), "e2e")];
const SELF = resolve(process.cwd(), "src/lib/__tests__/no-any.test.ts");

function sourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((entry) => {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.tsx?$/.test(path) && path !== SELF ? [path] : [];
  });
}

const FILES = ROOTS.flatMap(sourceFiles);

interface Offence {
  file: string;
  line: number;
  text: string;
}

function scan(pattern: RegExp): Offence[] {
  const found: Offence[] = [];
  for (const file of FILES) {
    const lines = readFileSync(file, "utf-8").split("\n");
    lines.forEach((text, index) => {
      if (pattern.test(text)) {
        found.push({ file: relative(process.cwd(), file), line: index + 1, text: text.trim() });
      }
    });
  }
  return found;
}

function report(offences: Offence[]): string {
  return offences.map((o) => `${o.file}:${o.line}: ${o.text}`).join("\n");
}

describe("apps/web", () => {
  it("has files to scan", () => {
    // A scan over nothing passes silently and proves nothing.
    expect(FILES.length).toBeGreaterThan(20);
  });

  it("contains no `any` annotation", () => {
    const offences = scan(/(:\s*any\b|<any>|\bas\s+any\b|\bAny\[\]|\[\s*any\b|,\s*any\s*[,>\]])/);
    expect(report(offences)).toBe("");
  });

  it("contains no TypeScript suppression comment", () => {
    const offences = scan(/@ts-(ignore|expect-error|nocheck)/);
    expect(report(offences)).toBe("");
  });

  it("does not disable the rules that forbid `any`", () => {
    const offences = scan(
      /eslint-disable[^\n]*(no-explicit-any|no-unsafe-assignment|no-unsafe-member-access|no-unsafe-call|no-unsafe-return|no-unsafe-argument)/,
    );
    expect(report(offences)).toBe("");
  });

  it("swallows no exception silently", () => {
    // The Python side's equivalent rule (CLAUDE.md house rule 3). An empty `catch` turns a failed
    // fetch into a blank screen with no explanation anywhere.
    const offences = scan(/catch\s*(\([^)]*\))?\s*\{\s*\}/);
    expect(report(offences)).toBe("");
  });
});

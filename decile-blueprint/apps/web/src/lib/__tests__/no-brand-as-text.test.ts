import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * `--brand` may be a surface. It may never be text (M36).
 *
 * `DESIGN.md` gives Baskfy one accent, `#ff4f00`. Against the cream canvas that measures
 * **3.27:1**, and white on it measures **3.30:1** — both below the 4.5:1 docs/11 §Accessibility
 * requires of anything a person has to read. It is a beautiful colour and it is not a legible one.
 *
 * `globals.css` resolves that by splitting the hue in two: `--brand` keeps the exact value for
 * *fills*, where coffee ink sits on it at 5.40:1, and `--accent` carries the same hue darkened to
 * `#c23b00` for anything that is text. `contrast.test.ts` asserts both pairs.
 *
 * **This file asserts the half a contrast checker cannot see.** A ratio test can only measure the
 * pairs it is handed; it has no idea whether somebody wrote `text-brand` on a paragraph. That is
 * the exact mistake the split invites — the brighter orange is the prettier one, and reaching for
 * it on a heading would silently ship 3.27:1 text with every colour assertion still green.
 *
 * ## What is banned, and what is not
 *
 * `text-brand` only.
 *
 * WCAG sets two different floors, and the split matters here. **Text** needs 4.5:1 (1.4.3), which
 * `#ff4f00` fails on the canvas at 3.27:1. **Graphical objects** — an icon, a chart line, a shape
 * in a logo — need 3:1 (1.4.11), which the same colour *passes*. So `fill-brand` on a rectangle
 * and `stroke-brand` on a chart series are correct uses of the token, not loopholes in this rule,
 * and `bg-brand` and `border-brand` were never in question.
 *
 * The one thing nobody may do is set type in it. Somebody who thinks they need to has to come
 * here and say why, which is the whole point.
 */
const SRC = resolve(process.cwd(), "src");

function walk(path: string): string[] {
  if (statSync(path).isFile()) return /\.tsx?$/.test(path) ? [path] : [];
  return readdirSync(path).flatMap((entry) => walk(join(path, entry)));
}

/** `text-brand`, `text-brand/70`, `hover:text-brand` — the token set on type. */
const BRAND_AS_TEXT = /\b(?:[a-z-]+:)*text-brand\b(?!-foreground)/;

describe("the brand orange is never rendered as text", () => {
  const files = walk(SRC).filter((path) => !path.includes("__tests__"));

  it("scans a real number of files", () => {
    // A path typo would make every assertion below vacuously true.
    expect(files.length).toBeGreaterThan(50);
  });

  it.each(files.map((path) => [relative(SRC, path), path]))("%s", (_name, path) => {
    const source = readFileSync(path, "utf8");
    const offender = source
      .split("\n")
      .findIndex((line) => BRAND_AS_TEXT.test(line) && !line.trimStart().startsWith("*"));
    expect(
      offender,
      `line ${offender + 1} sets type in --brand, which is 3.27:1 on the canvas and needs 4.5. ` +
        "Use text-accent — the same hue, at 5.31:1.",
    ).toBe(-1);
  });
});

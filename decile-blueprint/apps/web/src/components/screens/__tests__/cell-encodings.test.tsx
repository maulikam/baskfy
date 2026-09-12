import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  BUMPINESS_THRESHOLDS,
  BumpinessDots,
  RankBadge,
  REFERENCE_SCORE_SCALE,
  ReturnChip,
  ScoreBar,
  bumpinessBand,
  scoreBarScale,
} from "@/components/screens/cell-encodings";

const ENCODINGS_SOURCE = readFileSync(
  resolve(process.cwd(), "src/components/screens/cell-encodings.tsx"),
  "utf8",
);

/**
 * Every Tailwind palette family. A class naming one of these is a fixed colour: `bg-green-600` is
 * the same green on the cream canvas and on the near-black one, so it cannot be part of an
 * encoding that has to stay legible in both.
 */
const PALETTE_FAMILY =
  "slate|gray|grey|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose";

/**
 * `bg-green-600`, `hover:text-red-500`, `dark:border-zinc-800` — any utility that paints, on any
 * variant, in a literal palette step rather than a token.
 */
const PALETTE_CLASS = new RegExp(
  String.raw`\b(?:[a-z-]+:)*(?:bg|text|border|ring|fill|stroke|outline|decoration|divide|placeholder|caret|from|via|to|shadow)-(?:${PALETTE_FAMILY})-\d{2,3}\b`,
);

/** `#0a0a0a`, `rgb(10 10 10)`, `hsl(...)` written into the component instead of into the theme. */
const COLOUR_LITERAL = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|oklch|color-mix)\s*\(/;

/**
 * The lines of `source` that fix a colour in both themes.
 *
 * Comment bodies are skipped for the same reason `no-brand-as-text.test.ts` skips them: the prose
 * in this file cites `#ff4f00` and `--brand` by name to explain the split, and a rule that fired on
 * its own documentation would teach people to stop writing the documentation.
 */
function colourLiterals(source: string): string[] {
  return source
    .split("\n")
    .filter((line) => {
      const body = line.trimStart();
      if (body.startsWith("*") || body.startsWith("//") || body.startsWith("/*")) return false;
      return PALETTE_CLASS.test(line) || COLOUR_LITERAL.test(line);
    })
    .map((line) => line.trim());
}

describe("scoreBarScale", () => {
  it("derives the scale from the rows on screen", () => {
    expect(scoreBarScale([1, 2, 4])).toBe(4);
    expect(scoreBarScale([0.5, 1.5])).toBe(1.5);
  });

  it("falls back to REFERENCE_SCORE_SCALE when nothing positive is present", () => {
    expect(scoreBarScale([0, -1, Number.NaN])).toBe(REFERENCE_SCORE_SCALE);
  });
});

describe("ScoreBar proportional widths", () => {
  it("two scores above 3 paint different widths when scaled to the row set", () => {
    const scale = scoreBarScale([3.5, 5.13]);
    const { rerender } = render(<ScoreBar value={3.5} scale={scale} />);
    const narrow = screen.getByTestId("score-bar-fill").style.transform;

    rerender(<ScoreBar value={5.13} scale={scale} />);
    const wide = screen.getByTestId("score-bar-fill").style.transform;

    expect(narrow).not.toBe(wide);
    expect(wide).toBe("scaleX(1)");
    expect(narrow).toBe(`scaleX(${(3.5 / scale).toFixed(4)})`);
  });

  it("the largest score on screen paints full width", () => {
    render(<ScoreBar value={2} scale={4} />);
    expect(screen.getByTestId("score-bar-fill").style.transform).toBe("scaleX(0.5)");
    render(<ScoreBar value={4} scale={4} />);
    expect(screen.getAllByTestId("score-bar-fill")[1]?.style.transform).toBe("scaleX(1)");
  });

  it("negative and non-finite scores render without throwing", () => {
    expect(() => render(<ScoreBar value={-1} scale={3} />)).not.toThrow();
    expect(() => render(<ScoreBar value={Number.NaN} scale={3} />)).not.toThrow();
    expect(screen.getAllByTestId("score-bar-fill").every((node) => node.style.transform.includes("scaleX(0)"))).toBe(
      true,
    );
  });
});

describe("bumpinessBand", () => {
  it("uses absolute thresholds justified against seeded vol spread", () => {
    expect(BUMPINESS_THRESHOLDS).toEqual([0.25, 0.35, 0.45, 0.55]);
    expect(bumpinessBand(0.179)).toBe(1);
    expect(bumpinessBand(0.366)).toBe(3);
    expect(bumpinessBand(0.618)).toBe(5);
  });

  it("separates the calmest name from the median", () => {
    expect(bumpinessBand(0.179)).toBeLessThan(bumpinessBand(0.366)!);
  });
});

describe("BumpinessDots", () => {
  it("renders sr-only text for assistive tech", () => {
    render(<BumpinessDots value={0.366} />);
    expect(screen.getByText(/bumpiness 3 of 5/i)).toBeInTheDocument();
  });
});

describe("RankBadge", () => {
  it("exposes ranks 1–3 as visible numbers", () => {
    render(<RankBadge rank={1} />);
    expect(screen.getByText("1")).toBeVisible();
  });

  it("renders ranks above 3 as plain right-aligned figures", () => {
    render(<RankBadge rank={12} />);
    const badge = screen.getByText("12");
    expect(badge.className).toContain("text-right");
    expect(badge.className).not.toContain("rounded-full");
  });
});

/**
 * G8 of `gates/leaf-7.1.1-scorebar.md`: "Numbers stay `tabular-nums` and every encoding still
 * works in dark mode via semantic tokens only."
 *
 * The two halves are different kinds of claim and need different kinds of test.
 *
 * **`tabular-nums` is a rendered property**, so it is asserted by rendering. It matters because
 * every one of these encodings appears in a *column*: a rank, a score, a return. In a proportional
 * face a `1` is narrower than a `7`, so the digits of row 3 do not sit under the digits of row 2
 * and the eye loses the one comparison the column exists to support. The rule is therefore about
 * the element that carries the figure, not about the component, which is why each case below
 * reaches for the node holding the text rather than the wrapper.
 *
 * `BumpinessDots` is deliberately absent. Its only number lives in the `sr-only` string, which is
 * read aloud and never aligned against anything, and demanding a figure-width rule of text that is
 * never seen would be a test asserting the code rather than the spec (house rule 2).
 *
 * **Dark mode cannot be asserted by rendering at all**, and that is the trap this half exists to
 * avoid. jsdom applies no stylesheet, so a `.dark` class on the root would change nothing a test
 * could read; a render-based "dark mode test" would pass on a component hard-coded to `#0a0a0a`.
 * What actually makes an encoding survive the theme swap is that every colour it names is a
 * semantic token — `bg-muted`, `text-positive`, `bg-brand` — because `globals.css` redefines
 * exactly those under `.dark`. A literal (`#1a7f37`, `text-green-600`) is fixed in both themes,
 * and green-600 on the dark canvas is the failure this catches before anyone can see it.
 *
 * So the source is the subject. That is the same shape as `no-brand-as-text.test.ts`, for the same
 * reason: the property is a property of what was written, not of what one render happened to
 * produce.
 */
describe("G8: figures align and the palette survives a theme swap", () => {
  it("the score bar's figure is tabular", () => {
    render(<ScoreBar value={1.5} scale={3} />);
    expect(screen.getByText("1.50").className).toContain("tabular-nums");
  });

  it("the return chip's figure is tabular", () => {
    render(<ReturnChip text="+12.3%" value={0.123} />);
    expect(screen.getByText("+12.3%").className).toContain("tabular-nums");
  });

  it("both rank badge shapes keep tabular figures", () => {
    // The two branches are styled independently, so a regression can hit one and not the other.
    render(<RankBadge rank={1} />);
    expect(screen.getByText("1").className).toContain("tabular-nums");
    render(<RankBadge rank={12} />);
    expect(screen.getByText("12").className).toContain("tabular-nums");
  });

  it("names no colour that globals.css does not redefine under .dark", () => {
    const offenders = colourLiterals(ENCODINGS_SOURCE);
    expect(
      offenders,
      "these lines fix a colour in both themes; use a semantic token instead",
    ).toEqual([]);
  });

  it("scans the file it thinks it is scanning", () => {
    // Without this, a bad path makes the assertion above vacuously true.
    expect(ENCODINGS_SOURCE).toContain("export function ScoreBar");
    expect(ENCODINGS_SOURCE.length).toBeGreaterThan(2000);
  });

  it("would catch a hard-coded colour if one were added", () => {
    // The detector is itself asserted, or a typo in the regex silently disarms the rule.
    expect(colourLiterals(`  className="bg-green-600"`)).toHaveLength(1);
    expect(colourLiterals(`  style={{ color: "#1a7f37" }}`)).toHaveLength(1);
    expect(colourLiterals(`  className="bg-muted text-positive bg-brand"`)).toEqual([]);
  });
});

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Prompt 8 deliverable 1: "semantic positive/negative tokens that pass 4.5:1 in both light and
 * dark". docs/11 §Accessibility makes it a requirement for every colour that carries text —
 * "Contrast ≥ 4.5:1 in both themes, including the positive/negative number colours."
 *
 * This parses `globals.css` and computes the real WCAG 2.x ratios, rather than trusting a palette
 * that was eyeballed once. A token edited to a prettier shade fails here, which is the only way a
 * contrast requirement survives contact with a redesign.
 */
const CSS_PATH = resolve(process.cwd(), "src/app/globals.css");
const css = readFileSync(CSS_PATH, "utf-8");

const AA_NORMAL_TEXT = 4.5;

function block(selector: string): Record<string, string> {
  const index = css.indexOf(selector);
  expect(index, `${selector} block missing from globals.css`).toBeGreaterThan(-1);
  const open = css.indexOf("{", index);
  const close = css.indexOf("}", open);
  const body = css.slice(open + 1, close);
  const tokens: Record<string, string> = {};
  for (const line of body.split("\n")) {
    const match = /^\s*(--[a-z-]+):\s*(#[0-9a-fA-F]{6});/.exec(line);
    if (match?.[1] && match[2]) tokens[match[1]] = match[2];
  }
  return tokens;
}

function channelToLinear(channel: number): number {
  const value = channel / 255;
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
}

export function relativeLuminance(hex: string): number {
  const clean = hex.replace("#", "");
  const r = channelToLinear(Number.parseInt(clean.slice(0, 2), 16));
  const g = channelToLinear(Number.parseInt(clean.slice(2, 4), 16));
  const b = channelToLinear(Number.parseInt(clean.slice(4, 6), 16));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrastRatio(a: string, b: string): number {
  const first = relativeLuminance(a);
  const second = relativeLuminance(b);
  const lighter = Math.max(first, second);
  const darker = Math.min(first, second);
  return (lighter + 0.05) / (darker + 0.05);
}

const themes = {
  light: block(":root {"),
  dark: block(".dark {"),
} as const;

/** Every token that renders text, and the surfaces it is allowed to render on. */
const TEXT_ON_SURFACE: ReadonlyArray<{ token: string; surfaces: readonly string[] }> = [
  { token: "--foreground", surfaces: ["--background", "--card"] },
  { token: "--muted-foreground", surfaces: ["--background", "--card"] },
  { token: "--card-foreground", surfaces: ["--card"] },
  { token: "--popover-foreground", surfaces: ["--popover"] },
  { token: "--accent", surfaces: ["--background", "--card", "--accent-muted"] },
  { token: "--positive", surfaces: ["--background", "--card", "--positive-muted"] },
  { token: "--negative", surfaces: ["--background", "--card", "--negative-muted"] },
  { token: "--warning", surfaces: ["--background", "--card", "--warning-muted"] },
];

describe("the token palette", () => {
  it.each(Object.keys(themes))("defines every colour token in %s", (name) => {
    const tokens = themes[name as keyof typeof themes];
    for (const { token, surfaces } of TEXT_ON_SURFACE) {
      expect(tokens[token], `${token} missing in ${name}`).toBeDefined();
      for (const surface of surfaces) {
        expect(tokens[surface], `${surface} missing in ${name}`).toBeDefined();
      }
    }
  });

  describe.each(Object.keys(themes))("%s theme", (name) => {
    const tokens = themes[name as keyof typeof themes];

    it.each(TEXT_ON_SURFACE.flatMap(({ token, surfaces }) => surfaces.map((s) => [token, s])))(
      "%s on %s clears 4.5:1",
      (token, surface) => {
        const foreground = tokens[token];
        const background = tokens[surface];
        expect(foreground).toBeDefined();
        expect(background).toBeDefined();
        const ratio = contrastRatio(foreground as string, background as string);
        expect(
          ratio,
          `${token} on ${surface} in ${name} is ${ratio.toFixed(2)}:1`,
        ).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
      },
    );

    it("keeps the accent legible when it is a button's background", () => {
      // docs/08: "one accent for primary actions" — the label sits *on* the accent.
      const ratio = contrastRatio(
        tokens["--accent-foreground"] as string,
        tokens["--accent"] as string,
      );
      expect(ratio).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
    });

    it("keeps positive and negative distinguishable from each other", () => {
      // Not a WCAG rule, but the pair carries meaning (docs/11 §Accessibility), and two colours
      // of near-identical luminance are the same colour to a monochrome display.
      const positive = relativeLuminance(tokens["--positive"] as string);
      const negative = relativeLuminance(tokens["--negative"] as string);
      expect(Math.abs(positive - negative)).toBeGreaterThan(0.02);
    });
  });
});

describe("tabular numerals", () => {
  it("are set once at the root, so no numeric component can forget them", () => {
    // docs/08 §"Design principles": "one **tabular-figure** setting for every number".
    expect(css).toMatch(/html\s*\{[^}]*font-variant-numeric:\s*tabular-nums/s);
  });
});

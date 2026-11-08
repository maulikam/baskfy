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

/*
 * Four palettes, not two.
 *
 * `.band-dark` and `.band-light` are the painted bands — a marketing section set on `#0a0a0a` and
 * the ticker rail set on `#f8fafc`, each of which keeps its own colour whatever theme the reader
 * chose. They are surfaces with a palette, exactly like `:root` and `.dark`, and until 12 Sep 2026
 * they were neither of those things: each set two custom properties inline in a component and let
 * the rest fall through to a theme that was not the one on screen. Axe found twenty colour-contrast
 * failures on the landing page in both themes, and every one of them was a band.
 *
 * So they are parsed here with the other two. A palette that is invisible to this file is a palette
 * that gets checked by somebody opening a browser, which is what the 11 Sep note above is about.
 */
const themes = {
  light: block(":root {"),
  dark: block(".dark {"),
} as const;

/**
 * The painted bands, which are palettes too.
 *
 * `.band-dark` is a marketing section set on `#0a0a0a`; `.band-light` is the landing ticker rail
 * set on `#f8fafc`. Each keeps its colour whatever theme the reader chose, so each is a surface
 * with an ink ladder of its own — and until 12 Sep 2026 neither was one. Each component set two
 * custom properties inline and let everything else fall through to a theme that was not the one on
 * screen: `--muted` stayed light under the footer's `<Disclaimer/>` (1.69:1), the quiet tier had no
 * token so it was retyped as `#6f6f6f` on a `#141414` panel (3.67:1), and the ticker's muted copy
 * took the dark theme's #a6a6a6 onto a near-white rail (2.05:1). Twenty axe failures on the landing
 * page, in both themes, all of them a band.
 *
 * They are asserted here rather than in a browser, on the four tokens they exist to re-point.
 * `--positive`, `--brand` and the rest deliberately fall through to the reader's theme and are not
 * listed: a band re-points the greyscale, and meaning-carrying colour is checked above where it is
 * defined.
 */
const BANDS = {
  "band-dark": block(".band-dark {"),
  "band-light": block(".band-light {"),
} as const;

const BAND_INK = ["--foreground", "--card-foreground", "--muted-foreground", "--quiet-foreground"];
const BAND_SURFACES = ["--background", "--card", "--muted"];

/** Every token that renders text, and the surfaces it is allowed to render on. */
const TEXT_ON_SURFACE: ReadonlyArray<{ token: string; surfaces: readonly string[] }> = [
  { token: "--foreground", surfaces: ["--background", "--card"] },
  /* `--muted` was missing from this list until 11 Sep 2026, and that omission cost a real defect:
     `--muted-foreground` at #6f6f6f cleared 4.5 on the canvas and on a panel and read **4.41 on
     `--muted`** — the one surface it is named for. 540 assertions here were green while a pill in
     the Command Center failed AA in a browser. A token that is illegible on its own surface is
     the failure this file exists to prevent, so the surface it is named for is now asserted. */
  { token: "--muted-foreground", surfaces: ["--background", "--card", "--muted"] },
  /* The quiet tier, added 12 Sep 2026 for the eyebrows, captions and fine print that were being
     spelled `#6f6f6f` on the component instead. Same three surfaces as `--muted-foreground`, for
     the same reason: the failure that put it here was a quiet colour on a panel. */
  { token: "--quiet-foreground", surfaces: ["--background", "--card", "--muted"] },
  { token: "--card-foreground", surfaces: ["--card"] },
  { token: "--popover-foreground", surfaces: ["--popover"] },
  { token: "--accent", surfaces: ["--background", "--card", "--accent-muted"] },
  { token: "--positive", surfaces: ["--background", "--card", "--positive-muted"] },
  { token: "--negative", surfaces: ["--background", "--card", "--negative-muted"] },
  { token: "--warning", surfaces: ["--background", "--card", "--warning-muted"] },
  /* PC1. `--info` is the brief's "blue or violet ... sparingly for benchmarks and informational
     states". The Command Center renders it as an icon on `--info-muted` with `--foreground` text
     beside it, so BOTH pairs are asserted — an informational panel whose own text fails is worse
     than no panel, because it reads as decoration. */
  { token: "--info", surfaces: ["--background", "--card", "--info-muted"] },
  { token: "--foreground", surfaces: ["--info-muted"] },
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

    /**
     * M36. `--brand` is `DESIGN.md`'s `#ff4f00` at its exact brand value, and it exists precisely
     * because that value **fails** as text — 3.27:1 on the canvas. Splitting it out of `--accent`
     * is only honest if the token it moved into is checked too; otherwise the rule was not
     * satisfied, it was routed around.
     *
     * So the pair is asserted here in the form it is actually used: a fill, with a label on top.
     * Note what is deliberately NOT asserted — `--brand` against the page background — because
     * nothing renders it as text, and asserting it would fail a colour that is used correctly.
     * `no-brand-as-text.test.ts` is what holds that second half of the bargain.
     */
    it("keeps the brand fill legible under its own label", () => {
      const ratio = contrastRatio(
        tokens["--brand-foreground"] as string,
        tokens["--brand"] as string,
      );
      expect(
        ratio,
        `--brand-foreground on --brand in ${name} is ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
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

describe("the painted bands carry a palette of their own", () => {
  describe.each(Object.keys(BANDS))("%s", (name) => {
    const tokens = BANDS[name as keyof typeof BANDS];

    it("re-points every greyscale token a component inside it can ask for", () => {
      for (const token of [...BAND_INK, ...BAND_SURFACES, "--border", "--input"]) {
        expect(tokens[token], `${token} missing from ${name}`).toBeDefined();
      }
    });

    it.each(BAND_INK.flatMap((token) => BAND_SURFACES.map((surface) => [token, surface])))(
      "%s on %s clears 4.5:1",
      (token, surface) => {
        const ratio = contrastRatio(tokens[token] as string, tokens[surface] as string);
        expect(
          ratio,
          `${token} on ${surface} in ${name} is ${ratio.toFixed(2)}:1`,
        ).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
      },
    );

    it("keeps a step between the muted tier and the quiet one", () => {
      // Two names for one colour is a tier that has quietly stopped existing. The step is small in
      // light on purpose — AA leaves almost no room under `--muted-foreground` there — but it is a
      // step, and collapsing it is a design change, not a contrast fix.
      const muted = relativeLuminance(tokens["--muted-foreground"] as string);
      const quiet = relativeLuminance(tokens["--quiet-foreground"] as string);
      expect(Math.abs(muted - quiet)).toBeGreaterThan(0.002);
    });
  });

  it("matches the theme whose surface it paints, so a band is not a third palette", () => {
    // `.band-dark` IS the dark theme's canvas; `.band-light` IS the light one's. If they drift, a
    // reader crossing the boundary sees two different greys claiming to be the same thing.
    expect(BANDS["band-dark"]["--background"]).toBe(themes.dark["--background"]);
    expect(BANDS["band-dark"]["--card"]).toBe(themes.dark["--card"]);
    expect(BANDS["band-dark"]["--muted-foreground"]).toBe(themes.dark["--muted-foreground"]);
    expect(BANDS["band-dark"]["--quiet-foreground"]).toBe(themes.dark["--quiet-foreground"]);
    expect(BANDS["band-light"]["--background"]).toBe(themes.light["--background"]);
    expect(BANDS["band-light"]["--card"]).toBe(themes.light["--card"]);
    expect(BANDS["band-light"]["--muted-foreground"]).toBe(themes.light["--muted-foreground"]);
    expect(BANDS["band-light"]["--quiet-foreground"]).toBe(themes.light["--quiet-foreground"]);
  });
});

describe("tabular numerals", () => {
  it("are set once at the root, so no numeric component can forget them", () => {
    // docs/08 §"Design principles": "one **tabular-figure** setting for every number".
    expect(css).toMatch(/html\s*\{[^}]*font-variant-numeric:\s*tabular-nums/s);
  });
});

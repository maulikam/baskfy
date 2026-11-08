import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * `PORTFOLIO_REDESIGN.md` §11 acceptance criterion 7: **"No internal jargon from §8's left
 * column appears anywhere in the UI."**
 *
 * §8 is a rename table, and a rename table is only true on the day it is applied. The words it
 * bans are the words the two codebases think in — a `portfolio_sleeve` row really is a sleeve,
 * the ledger really is a book — so the next person to write a label reaches for them again
 * without noticing. This scans the shipped source instead of trusting that, the same way
 * `no-any.test.ts` scans for `any` and `packages/core/tests/test_no_escape_hatches.py` scans the
 * Python side: a house rule enforced only by whoever remembers it is a house rule with an off
 * switch.
 *
 * ## The distinction this test has to make
 *
 * §8 renames **what the user reads**, not what the code is called. `portfolio_sleeve` is a
 * database table, `SleeveListOut` is an API schema, `book-box` is a `data-testid` three test
 * suites locate by, and `/portfolios/[id]/sleeves` is a route. Renaming any of those breaks the
 * API contract for a cosmetic win. So the scan does not look at source text; it extracts the
 * subset of source text that reaches a reader's eyes, and looks only at that.
 *
 * ### What counts as user-visible
 *
 * 1. **JSX text nodes** — the characters between `>` and `<` in a `.tsx` file.
 * 2. **String and template literals**, in `.ts` and `.tsx` alike, since a label can be a
 *    `metadata.title`, a `PAGES` blurb, a `BOX_KIND_LABEL` value or a card's `reason` prop.
 *
 * ### What is excluded, and why
 *
 * - **Comments.** Stripped before anything else. A comment explaining *why* the sleeve table is
 *   called `portfolio_sleeve` is the documentation this rename must not delete.
 * - **Test files** (`__tests__/`, `*.test.ts(x)`) and this file itself. A fixture named
 *   `ruleBox` is not a label.
 * - **Literals containing `/`** — import specifiers, routes, API paths, URLs. `"@/lib/portfolios/book"`
 *   and `` `/portfolios/${id}/sleeves` `` are addresses, not prose. (Cost: a visible string that
 *   contains a slash — "TWR / XIRR" — is not scanned. A false negative, never a false positive.)
 * - **Identifier-shaped literals** — once `${…}` interpolations are removed, anything matching
 *   `lower_snake` / `kebab-case` / a single lowercase token: `"book-box"`, `"cash-sleeve"`,
 *   `"manual"`, `"spans"`, `` `sleeve-${id}` ``. These are testids, enum tags, form `name`s and
 *   CSS classes. Every real label in this app is either capitalised or a sentence.
 * - **Literals attached to a non-visible JSX attribute** — `className`, `href`, `data-*`, `key`,
 *   `id`, `name`, `type`, `role`, `value`, … `aria-*` is deliberately *not* excluded: an
 *   `aria-label` is read aloud, so it is as visible as anything on the screen.
 *
 * ### Word boundaries, and the words that survive them
 *
 * Every pattern is `\b`-anchored, which is what keeps "tool**box**", "**book**mark",
 * "check**box**", "**divide**nd" and Tailwind's `divide-y` out of the results: there is no word
 * boundary inside a compound. The plural and inflected forms *are* matched — "boxes", "sleeves",
 * "nested", "nesting" — because §8 bans the concept, not the singular spelling. `describe` blocks
 * at the bottom of this file assert both directions, so a future widening of a pattern that
 * starts catching "toolbox" fails here rather than in review.
 *
 * ### The allowed exceptions
 *
 * `ALLOWED` carries phrases that contain a banned word in an unrelated, ordinary meaning. Adding
 * to it is the sanctioned escape hatch — a phrase and a reason, never a widened `\b` — and it
 * matches the `tools/check-namespace.sh` convention the root CLAUDE.md sets for the same problem.
 */

const ROOT = resolve(process.cwd(), "src");
const SELF = resolve(ROOT, "lib/__tests__/no-jargon.test.ts");

function sourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((entry) => {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) {
      return entry === "__tests__" ? [] : sourceFiles(path);
    }
    if (!/\.tsx?$/.test(path)) return [];
    if (/\.test\.tsx?$/.test(path)) return [];
    return path === SELF ? [] : [path];
  });
}

const FILES = sourceFiles(ROOT);

/**
 * Phrases that contain a banned word in a meaning §8 never legislated over.
 *
 * Each one is an exact phrase with a reason. Widening a pattern to silence a hit is forbidden;
 * this list is where a real exception goes.
 */
const ALLOWED: { phrase: RegExp; reason: string }[] = [
  {
    // The P/B ratio. "Book value" is the company's net assets on paper — public financial
    // vocabulary a reader brings with them, not this product's word for its own ledger.
    phrase: /\bprice vs book\b|\bbook value\b/gi,
    reason: "P/B ratio — the company's book value, not our ledger",
  },
  {
    // The literal checkbox on the mark-invested form. "Box" here is the control the reader ticks.
    phrase: /\btick the box\b/gi,
    reason: "the checkbox on the confirmation form",
  },
  {
    // The CSS property, named inside a Tailwind `transition-[…]` list. Not prose at all, but a
    // class string built by `cva`/`cn` has no attribute in front of it to filter on.
    phrase: /\bbox-shadow\b/gi,
    reason: "the CSS property in a Tailwind transition list",
  },
  {
    // Arithmetic. "CAGR divided by the max drawdown" is long division, not §8's split-a-portfolio
    // action — which is why the exception is the two-word phrase and not the verb.
    phrase: /\bdivided by\b/gi,
    reason: "arithmetic division, not the sub-portfolio action",
  },
  {
    // The English idiom for something opaque, used to say this product is the opposite of one.
    phrase: /\bblack box\b/gi,
    reason: "the idiom, in a sentence denying it",
  },
];

interface Banned {
  /** §8's left column, verbatim. */
  readonly jargon: string;
  /** §8's right column — what the message tells the author to write instead. */
  readonly instead: string;
  readonly pattern: RegExp;
}

const BANNED: Banned[] = [
  { jargon: "Box", instead: "Portfolio", pattern: /\bboxe?s?\b/i },
  { jargon: "Book", instead: "Portfolio group", pattern: /\bbooks?\b/i },
  { jargon: "Sleeve", instead: "Allocation", pattern: /\bsleeves?\b/i },
  { jargon: "Divide", instead: "Create sub-portfolio", pattern: /\bdivide[sd]?\b|\bdividing\b/i },
  { jargon: "File under", instead: "Move to group", pattern: /\bfiled?\b[^.]{0,24}\bunder\b/i },
  { jargon: "Spans brokers", instead: "Connected to N brokers", pattern: /\bspans?\b[^.]{0,20}\bbrokers?\b/i },
  { jargon: "Your rule", instead: "My strategy / My screen", pattern: /\byour rule\b/i },
  { jargon: "Run by hand", instead: "Manual holdings", pattern: /\brun(s|ning)?\s+by\s+hand\b/i },
  { jargon: "Nest", instead: "(drop the concept from v1 UI)", pattern: /\bnest(s|ed|ing)?\b/i },
];

/** JSX attributes whose value never reaches a reader. `aria-*` is absent on purpose. */
const NON_VISIBLE_ATTRIBUTES =
  /(className|class|id|htmlFor|href|src|key|name|type|role|style|variant|size|value|autoComplete|spellCheck|slot|rel|target|method|action|as|icon|section|data-[\w-]+)\s*=\s*\{?\s*$/;

/** Line comments, block comments and JSX `{/* … *\/}` comments, replaced by blanks. */
function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/^[ \t]*\/\/.*$/gm, " ")
    .replace(/([;,)}])\s*\/\/.*$/gm, "$1");
}

/** Interpolation-free view of a literal, so `` `sleeve-${id}` `` reads as the identifier it is. */
function withoutInterpolations(text: string): string {
  return text.replace(/\$\{[^{}]*\}/g, "");
}

function isIdentifierShaped(text: string): boolean {
  return /^[a-z0-9]*([-_.][a-z0-9]*)*$/.test(withoutInterpolations(text).trim());
}

export interface VisibleString {
  readonly text: string;
  readonly line: number;
}

/** Every run of characters in `source` that a reader could end up looking at. */
export function visibleStrings(source: string, isTsx: boolean): VisibleString[] {
  const stripped = stripComments(source);
  const lineOf = (index: number): number => stripped.slice(0, index).split("\n").length;
  const found: VisibleString[] = [];

  // 1. Quoted string and template literals.
  const literal = /(["'`])((?:\\.|(?!\1)[^\\])*)\1/g;
  let match: RegExpExecArray | null;
  while ((match = literal.exec(stripped)) !== null) {
    const text = match[2] ?? "";
    if (text.trim() === "" || text.includes("/")) continue;
    if (isIdentifierShaped(text)) continue;
    const before = stripped.slice(Math.max(0, match.index - 48), match.index);
    if (NON_VISIBLE_ATTRIBUTES.test(before)) continue;
    found.push({ text, line: lineOf(match.index) });
  }

  // 2. JSX text nodes. Punctuation that only appears in code — `;`, `=`, brackets — rules out
  //    the arrow-function and generic-parameter `>` that would otherwise open a false capture.
  if (isTsx) {
    const node = /> *([^<>{};=()[\]]+?) *</g;
    while ((match = node.exec(stripped)) !== null) {
      const text = (match[1] ?? "").replace(/\s+/g, " ").trim();
      if (!/[A-Za-z]{2}/.test(text)) continue;
      found.push({ text, line: lineOf(match.index) });
    }
  }

  return found;
}

export interface Offence {
  file: string;
  line: number;
  jargon: string;
  instead: string;
  text: string;
}

export function scan(files: string[]): Offence[] {
  const offences: Offence[] = [];
  for (const file of files) {
    const source = readFileSync(file, "utf-8");
    for (const { text, line } of visibleStrings(source, file.endsWith(".tsx"))) {
      let residue = text;
      for (const { phrase } of ALLOWED) residue = residue.replace(phrase, " ");
      for (const { jargon, instead, pattern } of BANNED) {
        if (pattern.test(residue)) {
          offences.push({ file: relative(process.cwd(), file), line, jargon, instead, text });
        }
      }
    }
  }
  return offences;
}

function report(offences: Offence[]): string {
  return offences
    .map((o) => `${o.file}:${o.line}: "${o.jargon}" -> "${o.instead}" in: ${o.text}`)
    .join("\n");
}

describe("PORTFOLIO_REDESIGN.md §8 — the renamed words never reach a reader", () => {
  it("has files to scan", () => {
    // A scan over nothing passes silently and proves nothing.
    expect(FILES.length).toBeGreaterThan(100);
  });

  it("finds no §8 jargon in any user-visible string", () => {
    expect(report(scan(FILES))).toBe("");
  });
});

describe("the scanner is precise about what it calls user-visible", () => {
  const offend = (source: string, tsx = true): string => {
    const offences: Offence[] = [];
    for (const { text } of visibleStrings(source, tsx)) {
      let residue = text;
      for (const { phrase } of ALLOWED) residue = residue.replace(phrase, " ");
      for (const { jargon, instead, pattern } of BANNED) {
        if (pattern.test(residue)) offences.push({ file: "-", line: 0, jargon, instead, text });
      }
    }
    return offences.map((o) => o.jargon).join(",");
  };

  it("catches a JSX text node", () => {
    expect(offend("<p>No boxes in this book yet. Divide it.</p>")).toBe("Box,Book,Divide");
  });

  it("catches a label string and an aria-label", () => {
    expect(offend('const L = { rule: "Your rule" };', false)).toBe("Your rule");
    expect(offend('<select aria-label="Sleeve source" />')).toBe("Sleeve");
  });

  it("catches the phrases as well as the single words", () => {
    expect(offend("<p>File under</p>")).toBe("File under");
    expect(offend("<span>Spans 2 brokers</span>")).toBe("Spans brokers");
    expect(offend("<p>names you run by hand</p>")).toBe("Run by hand");
    expect(offend("<h2>How your portfolios nest</h2>")).toBe("Nest");
  });

  it("does not fire on compounds that merely contain a banned word", () => {
    // The whole reason every pattern is `\b`-anchored.
    for (const word of ["toolbox", "bookmark", "checkbox", "combobox", "textbox", "dividend"]) {
      expect(offend(`<p>a ${word} here</p>`), word).toBe("");
    }
    expect(offend('<ul className="divide-y divide-border">a b</ul>')).toBe("");
  });

  it("does not fire on identifiers, testids, routes or imports", () => {
    expect(offend('import { composeBook } from "@/lib/portfolios/book";', false)).toBe("");
    expect(offend('<Link href={`/portfolios/${id}/sleeves`} data-testid="book-box">go</Link>')).toBe("");
    expect(offend('const id = `sleeve-${portfolio.id}`;', false)).toBe("");
    expect(offend('type K = "manual" | "screen" | "spans";', false)).toBe("");
    expect(offend('<input name="cash-sleeve" className="box-border" />')).toBe("");
  });

  it("does not fire inside comments, which are where the schema names are explained", () => {
    expect(offend("/** A sleeve is a box in the book. */\nconst x = 1;", false)).toBe("");
    expect(offend("// nested boxes divide the book\nconst y = 2;", false)).toBe("");
    expect(offend("<p>{/* a sleeve of the book */}ok</p>")).toBe("");
  });

  it("honours the documented exceptions and nothing wider", () => {
    expect(offend('<th>Price vs book</th>')).toBe("");
    expect(offend('setError("Tick the box only after you have placed the orders.");', false)).toBe("");
    // The exception is the phrase, not the word: "book" on its own is still banned.
    expect(offend("<p>we sync the book into your portfolio</p>")).toBe("Book");
  });
});

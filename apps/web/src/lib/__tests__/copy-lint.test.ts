import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Prompt 18's third acceptance criterion:
 *
 *     "No page claims or implies investment advice; add a copy-lint test with a banned-phrase list
 *      ('guaranteed returns', 'buy now', 'recommendation', 'advice')."
 *
 * and docs/11 §"Compliance & legal (India)": "No buy/sell recommendations, no target prices, no
 * 'advice' language anywhere in copy."
 *
 * ## The problem with a plain banned-phrase list
 *
 * Two of the four phrases the prompt names — "recommendation" and "advice" — appear in copy we are
 * *required* to publish. The `<Disclaimer/>` component's sentence, which docs/11 mandates on every
 * analytics surface, contains both:
 *
 *     "…not investment **advice**, and no output is a **recommendation** to buy or sell any
 *      security."
 *
 * A list that banned the substring would fail on the disclaimer itself, so the only way to keep it
 * green would be to exempt the very files the rule exists to police.
 *
 * ## What is asserted instead
 *
 * **Every occurrence must be negated.** A window of text before each hit is searched for a
 * negator — "not", "no", "never", "without", "does not constitute", "cannot". A negated occurrence
 * is a disclaimer; an un-negated one is a claim. `is not investment advice` passes;
 * `expert investment advice` fails.
 *
 * The phrases with no legitimate negated form — "guaranteed returns", "buy now", "sure shot",
 * "multibagger", "target price" — are banned outright, negation or not, because there is no
 * sentence containing them that this product should be publishing.
 *
 * ## What is scanned
 *
 * Everything that renders as prose to a visitor: the marketing, content and legal routes, their
 * components and copy modules, and every MDX document. Source *comments* are excluded — a code
 * comment explaining why a word is banned is not copy, and scanning them would make this test
 * unable to describe itself.
 */

const WEB_ROOT = resolve(process.cwd(), "src");

/**
 * The routes and modules whose text a visitor reads.
 *
 * Not the whole of `src`: the screener's form labels, the error catalogue and the admin surface
 * are operational vocabulary, not marketing copy, and "Recommendation" would be as wrong there —
 * but a false positive from an API error string would make the useful part of this test noise.
 * Widening this list is cheap; the four directories below are where copy actually lives.
 */
const SCANNED = [
  join(WEB_ROOT, "app", "(marketing)"),
  join(WEB_ROOT, "content"),
  join(WEB_ROOT, "components", "marketing"),
  join(WEB_ROOT, "components", "consent"),
  join(WEB_ROOT, "lib", "marketing"),
  join(WEB_ROOT, "components", "data", "disclaimer.tsx"),
];

const SOURCE_EXTENSIONS = /\.(tsx?|mdx)$/;

function walk(path: string): string[] {
  if (statSync(path).isFile()) return SOURCE_EXTENSIONS.test(path) ? [path] : [];
  return readdirSync(path).flatMap((entry) => walk(join(path, entry)));
}

const FILES = SCANNED.flatMap(walk);

/**
 * Blank out block comments, line comments and JSX/MDX comments, **preserving line numbers**.
 *
 * A multi-line comment is replaced by its own newlines rather than by a space, so a violation's
 * reported line still points at the right line of the file. Getting that wrong makes the failure
 * message actively misleading, which is worse than not reporting a line at all.
 *
 * Crude on purpose beyond that: a `//` inside a string literal (a URL) takes the rest of the line
 * with it, which can only ever *remove* text and therefore only ever weaken the scan for that
 * line. The alternative is a parser, and a parser for six directories of prose is the wrong trade.
 */
function blankPreservingLines(match: string): string {
  return match.replace(/[^\n]/g, " ");
}

function copyOnly(source: string): string {
  return source
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, blankPreservingLines)
    .replace(/\/\*[\s\S]*?\*\//g, blankPreservingLines)
    .replace(/^\s*\/\/.*$/gm, blankPreservingLines)
    .replace(/\s+\/\/\s.*$/gm, blankPreservingLines);
}

interface Hit {
  file: string;
  line: number;
  text: string;
}

/** Words that turn a claim into a disclaimer. Searched in the text *preceding* the phrase. */
const NEGATORS = [
  "not",
  "no",
  "never",
  "nor",
  "nothing",
  "none",
  "without",
  "cannot",
  "isn't",
  "doesn't",
  "neither",
  "refuses",
];

/**
 * The scope a negation governs: **the sentence**, not a fixed number of characters.
 *
 * A character window gets this wrong in the one place it matters most. The disclaimer's own
 * §1 reads "…is **not** registered with the Securities and Exchange Board of India in any
 * capacity — not as an investment **adviser** under the SEBI (Investment Advisers) Regulations,
 * 2013, not as a research analyst…". The governing "not" is a hundred characters and a line break
 * away from the third occurrence, and no fixed window is both wide enough for that and narrow
 * enough to be meaningful.
 *
 * A sentence boundary is `.`, `!`, `?` or `:` followed by whitespace, or a blank line, or the
 * start of a Markdown heading or list item. `Regulations, 2013,` is not one, so the whole list is
 * correctly read as a single negated sentence.
 */
const SENTENCE_BOUNDARY = /(?:[.!?:]["'\u201d)\]]?(?:\s|,|$)|\n\s*\n|\n\s*[#>*-]\s)/g;

function sentenceBefore(source: string, offset: number): string {
  const before = source.slice(0, offset);
  let start = 0;
  const matcher = new RegExp(SENTENCE_BOUNDARY.source, "g");
  let match: RegExpExecArray | null;
  while ((match = matcher.exec(before)) !== null) start = match.index + match[0].length;
  return before.slice(start);
}

function isNegated(before: string): boolean {
  const window = before.toLowerCase();
  return NEGATORS.some((negator) => new RegExp(`\\b${negator}\\b`).test(window));
}

/**
 * The one un-negated form that is allowed: pointing the reader at somebody who *is* registered.
 *
 * "Consider taking advice from a SEBI-registered investment adviser" is not a claim to give
 * advice — it is the opposite, and it is the sentence a regulator would want to see. Requiring
 * "SEBI-registered" within the same clause keeps the carve-out narrow: it cannot be used to
 * describe *us*, because we are not registered and every sentence that says so is negated anyway.
 */
function pointsAtARegisteredAdviser(sentence: string): boolean {
  return /sebi[- ]registered/i.test(sentence);
}

/**
 * A question is not a claim.
 *
 * The FAQ's own heading is "Is this investment advice?", answered "No." — asking the question is
 * the opposite of asserting it, and a lint that forced the question to be rephrased would push the
 * answer further from the reader. The check is deliberately narrow: the *sentence* must end in a
 * question mark, so a sentence that merely contains one elsewhere is still a claim.
 *
 * The always-banned phrases ignore this carve-out entirely, so "Want guaranteed returns?" is still
 * a failure.
 */
function isAQuestion(sentence: string): boolean {
  return /\?["'\u201d)\]]?$/.test(sentence.trim());
}

/** The whole sentence a match sits in — used for the negation test and for the report. */
function sentenceAround(source: string, offset: number): string {
  const before = sentenceBefore(source, offset);
  const rest = source.slice(offset);
  const end = new RegExp(SENTENCE_BOUNDARY.source).exec(rest);
  return `${before}${rest.slice(0, end ? end.index + 1 : rest.length)}`.replace(/\s+/g, " ").trim();
}

function scan(pattern: RegExp, { allowNegated }: { allowNegated: boolean }): Hit[] {
  const hits: Hit[] = [];
  for (const file of FILES) {
    /* Scanned as one string rather than line by line: prose wraps, and a sentence is the unit a
       negation governs. The line number is recovered from the offset for the report. */
    const source = copyOnly(readFileSync(file, "utf-8"));
    const matcher = new RegExp(pattern.source, "gi");
    let match: RegExpExecArray | null;
    while ((match = matcher.exec(source)) !== null) {
      const sentence = sentenceAround(source, match.index);
      if (allowNegated && isNegated(sentenceBefore(source, match.index))) continue;
      if (allowNegated && pointsAtARegisteredAdviser(sentence)) continue;
      if (allowNegated && isAQuestion(sentence)) continue;
      const line = source.slice(0, match.index).split("\n").length;
      hits.push({ file: relative(process.cwd(), file), line, text: sentence });
    }
  }
  return hits;
}

function format(hits: Hit[]): string {
  return hits.map((hit) => `${hit.file}:${hit.line}: ${hit.text}`).join("\n");
}

describe("the copy never claims to give investment advice", () => {
  /** Prompt 18's own list, minus the two that have a legitimate negated form. */
  const ALWAYS_BANNED = [
    "guaranteed return",
    "guaranteed profit",
    "assured return",
    "risk-free return",
    "buy now",
    "sell now",
    "sure shot",
    "multibagger",
    "multi-bagger",
    "best stocks to buy",
    "stock tips",
    "our picks",
    "we recommend",
    "you should buy",
    "will outperform",
    "beat the market",
  ];

  it.each(ALWAYS_BANNED)("never says %s", (phrase) => {
    const hits = scan(new RegExp(phrase.replace(/[-]/g, "[- ]")), { allowNegated: false });
    expect(format(hits)).toBe("");
  });

  /**
   * Permitted only in a negated clause, or in a clause pointing at a registered adviser.
   *
   * Prompt 18 names "recommendation" and "advice". "target price" is here rather than in the
   * outright ban above for the same reason they are: docs/11 §Compliance says "no target prices",
   * and a disclaimer has to be able to say the words it is disclaiming.
   */
  const NEGATION_ONLY = [
    "advice",
    "adviser",
    "advisory",
    "recommendation",
    "recommends",
    "target price",
    "price target",
  ];

  it.each(NEGATION_ONLY)("only ever uses %s in a negated clause", (phrase) => {
    const hits = scan(new RegExp(`\\b${phrase.replace(" ", "\\s+")}s?\\b`), {
      allowNegated: true,
    });
    expect(format(hits)).toBe("");
  });

  it("scanned a meaningful amount of copy", () => {
    // A scan over nothing passes silently and proves nothing.
    expect(FILES.length).toBeGreaterThan(15);
  });

  it("catches an un-negated claim, so a green run means something", () => {
    /* The lint is only worth having if it would fail. This drives the matcher directly over a
       sentence nobody should ever ship, and asserts it is caught. */
    const line = "Our expert investment advice tells you what to buy.";
    const matcher = new RegExp("\\badvice\\b", "gi");
    const match = matcher.exec(line);
    expect(match).not.toBeNull();
    expect(isNegated(line.slice(0, match?.index ?? 0))).toBe(false);
  });

  it("passes a negated clause, so the disclaimer itself is not a violation", () => {
    const line = "Everything here is factual analysis, not investment advice.";
    const match = new RegExp("\\badvice\\b", "gi").exec(line);
    expect(isNegated(line.slice(0, match?.index ?? 0))).toBe(true);
  });

  it("treats a question as a question and a statement as a claim", () => {
    expect(isAQuestion("Is this investment advice?")).toBe(true);
    expect(isAQuestion("This is investment advice.")).toBe(false);
    // A question mark elsewhere in the sentence does not buy an exemption.
    expect(isAQuestion("Advice? We give the best investment advice.")).toBe(false);
  });
});

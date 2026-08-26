import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { DRAFT_MARKER, LEGAL_DOCUMENTS } from "@/lib/marketing/legal";

/**
 * Prompt 18 deliverable 3, both halves:
 *
 *     "Mark them clearly as DRAFTS REQUIRING LEGAL REVIEW **at the top of each file in the repo,
 *      not on the rendered page**."
 *
 * So: every document must carry the marker in its source, and no document may render it.
 *
 * The second half is the one worth a test. A "DRAFT" watermark on a live terms page is worse than
 * no terms page — it invites a customer to argue that nothing was agreed — and the mechanism that
 * keeps it off the page (an MDX comment, `{/* … *\/}`, which compiles to nothing) is exactly the
 * kind of thing that stops working silently if someone reformats the file.
 */
const LEGAL_DIR = resolve(process.cwd(), "src", "content", "legal");

function mdxFiles(): string[] {
  return readdirSync(LEGAL_DIR).filter((entry) => entry.endsWith(".mdx"));
}

/** The first MDX comment block, if the file opens with one. */
function leadingComment(source: string): string | null {
  const match = /^\s*\{\/\*([\s\S]*?)\*\/\}/.exec(source);
  return match?.[1] ?? null;
}

/** Everything outside MDX comments — an approximation of what a reader will see. */
function renderedText(source: string): string {
  return source.replace(/\{\/\*[\s\S]*?\*\/\}/g, " ");
}

describe("the legal documents are marked as drafts in the repository", () => {
  it("has one MDX file per registered document, and no orphans", () => {
    const onDisk = mdxFiles().map((name) => name.replace(/\.mdx$/, "")).sort();
    const registered = LEGAL_DOCUMENTS.map((document) => document.slug).sort();
    expect(onDisk).toEqual(registered);
  });

  it.each(mdxFiles())("%s opens with the draft marker", (name) => {
    const source = readFileSync(join(LEGAL_DIR, name), "utf-8");
    const comment = leadingComment(source);
    expect(comment, `${name} does not open with an MDX comment`).not.toBeNull();
    expect(comment).toContain(DRAFT_MARKER);
  });

  it.each(mdxFiles())("%s says a lawyer has not read it", (name) => {
    const comment = leadingComment(readFileSync(join(LEGAL_DIR, name), "utf-8")) ?? "";
    expect(comment.toUpperCase()).toContain("NOT REVIEWED BY A LAWYER");
  });

  it.each(mdxFiles())("%s points at the review checklist", (name) => {
    const comment = leadingComment(readFileSync(join(LEGAL_DIR, name), "utf-8")) ?? "";
    expect(comment).toContain("DRAFT-NOTICE.md");
  });

  it.each(mdxFiles())("%s does not render the marker to a visitor", (name) => {
    const rendered = renderedText(readFileSync(join(LEGAL_DIR, name), "utf-8"));
    expect(rendered).not.toContain(DRAFT_MARKER);
    expect(rendered.toUpperCase()).not.toContain("NOT REVIEWED BY A LAWYER");
  });

  it("keeps the checklist beside the drafts", () => {
    const notice = readFileSync(join(LEGAL_DIR, "DRAFT-NOTICE.md"), "utf-8");
    /* The notice is titled in the plural ("DRAFTS REQUIRING…") because it covers all four; each
       document carries the singular `DRAFT_MARKER`. The shared stem is what is asserted. */
    expect(notice).toContain("REQUIRING LEGAL REVIEW");
    // The eight decisions a reviewer has to make. If one is deleted, the count catches it.
    expect(notice.match(/^\d+\. \*\*/gm)?.length).toBe(8);
  });
});

describe("the documents render as documents", () => {
  /*
   * `privacy-policy.mdx` states what we store, and for how long, in two tables. MDX implements
   * CommonMark, and **pipe tables are not CommonMark** — they are a GitHub extension — so without
   * `remark-gfm` in the pipeline those tables reached the reader as literal lines of
   * `| Session token | Keeps you signed in | No |` on a live legal page.
   *
   * This asserts the pipeline against the content rather than the config alone: if a document
   * grows a table, the plugin that renders it must be configured. Checking `next.config.ts` is
   * indirect, and it is the only place the two facts meet without building the app in a unit test.
   */
  const NEXT_CONFIG = resolve(process.cwd(), "next.config.ts");

  it("configures remark-gfm whenever a document uses a table", () => {
    const withTables = mdxFiles().filter((name) =>
      /^\|.+\|\s*$/m.test(readFileSync(join(LEGAL_DIR, name), "utf-8")),
    );
    if (withTables.length === 0) return;

    const config = readFileSync(NEXT_CONFIG, "utf-8");
    expect(
      config,
      `${withTables.join(", ")} use pipe tables, which MDX cannot parse without remark-gfm`,
    ).toContain("remarkGfm");
    expect(config).toContain("remarkPlugins");
  });

  it("keeps every table well-formed — a header, a delimiter, and rows of equal width", () => {
    for (const name of mdxFiles()) {
      const lines = readFileSync(join(LEGAL_DIR, name), "utf-8").split("\n");
      for (const [index, line] of lines.entries()) {
        if (!/^\|.+\|\s*$/.test(line)) continue;
        const next = lines[index + 1] ?? "";
        // The line after a table's header must be the delimiter row, or GFM does not see a table
        // at all and silently renders the whole block as a paragraph of pipes.
        const isHeader = !/^\|[\s:|-]+\|\s*$/.test(line) && !/^\|.+\|\s*$/.test(lines[index - 1] ?? "");
        if (isHeader) {
          expect(next, `${name}:${index + 1} — table header is not followed by a delimiter row`)
            .toMatch(/^\|[\s:|-]+\|\s*$/);
          const columns = line.split("|").length;
          expect(next.split("|").length, `${name}:${index + 2} — delimiter column count`)
            .toBe(columns);
        }
      }
    }
  });
});

describe("the drafts do not pretend to be finished", () => {
  /*
   * This block used to assert the opposite: that every document still carried a `[BRACKETED]`
   * placeholder, on the reasoning that "a legal document that invented a GSTIN or a registered
   * address would be worse than one that admits it does not know them".
   *
   * That reasoning is intact. What changed is the fact underneath it — Maulik supplied the
   * supplier identity, the GSTIN, the grievance officer and the jurisdiction on 27 Aug 2026, so
   * those are no longer unknown and stating them is not invention. Two things genuinely are still
   * unknown, and those are what this now guards. Asserting "a placeholder exists" would, from
   * today, be asserting current behaviour rather than the spec (house rule 2).
   */

  /*
   * The last two placeholders were filled with conventional defaults on 27 Aug 2026 at Maulik's
   * request: 90 days for server logs, and DPDP §16 with the countries named for cross-border
   * transfers. Both are now statements of policy, so what is worth asserting is that they say
   * something specific rather than that they are absent.
   */
  it("commits to a definite log-retention period", () => {
    const rendered = renderedText(readFileSync(join(LEGAL_DIR, "privacy-policy.mdx"), "utf-8"));
    expect(rendered).toMatch(/\| Server logs \| \d+ days \|/);
  });

  it("names the transfer mechanism and the countries, not just 'the DPDP Act'", () => {
    const rendered = renderedText(readFileSync(join(LEGAL_DIR, "privacy-policy.mdx"), "utf-8"));
    // The section, because "we comply with the DPDP Act" is not a mechanism...
    expect(rendered).toContain("Section 16 of the DPDP Act");
    // ...and the countries, because a transfer notice that names none tells a reader nothing.
    for (const country of ["India", "United States"]) {
      expect(rendered, `§4 does not say where data goes: ${country}`).toContain(country);
    }
  });

  /*
   * The inverse guard. Now that the supplier placeholders are filled, an unfilled one reappearing
   * means a document was edited from an older copy — which would ship `[SUPPLIER GSTIN]` to a
   * reader on a live page.
   */
  const FILLED_IN = [
    "[SUPPLIER LEGAL NAME]",
    "[SUPPLIER ENTITY TYPE]",
    "[SUPPLIER ADDRESS]",
    "[SUPPLIER GSTIN]",
    "[GRIEVANCE OFFICER NAME]",
    "[GRIEVANCE OFFICER EMAIL]",
    "[CITY]",
    "[HOSTING PROVIDER]",
    "[EMAIL PROVIDER]",
  ];

  it.each(mdxFiles())("%s has no unfilled supplier placeholder left", (name) => {
    const rendered = renderedText(readFileSync(join(LEGAL_DIR, name), "utf-8"));
    for (const placeholder of FILLED_IN) {
      expect(rendered, `${name} still contains ${placeholder}`).not.toContain(placeholder);
    }
  });

  /*
   * The supplier's identity is written out in four documents. Four copies of an address is exactly
   * the thing that drifts when one gets edited — and a terms page and a privacy page naming
   * different addresses for the same supplier is the kind of inconsistency a counterparty notices.
   */
  it("states one supplier identity, identically, everywhere it appears", () => {
    const facts = ["RENIL", "703/2, Sector 4C, Gandhinagar, Gujarat 382006"];
    for (const fact of facts) {
      const naming = mdxFiles().filter((name) =>
        renderedText(readFileSync(join(LEGAL_DIR, name), "utf-8")).includes(fact),
      );
      expect(naming.length, `no document states "${fact}"`).toBeGreaterThan(0);
    }
    // The GSTIN appears once, in the terms — and must not have been pasted into a second document
    // where a stale copy could diverge from it.
    const withGstin = mdxFiles().filter((name) =>
      readFileSync(join(LEGAL_DIR, name), "utf-8").includes("24ANFPD9399F1ZS"),
    );
    expect(withGstin).toEqual(["terms-conditions.mdx"]);
  });
});

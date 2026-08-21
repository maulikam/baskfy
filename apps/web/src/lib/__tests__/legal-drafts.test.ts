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

describe("the drafts do not pretend to be finished", () => {
  it.each(mdxFiles())("%s leaves the unknown facts as bracketed placeholders", (name) => {
    /* A legal document that invented a GSTIN or a registered address would be worse than one that
       admits it does not know them: the invented one reads as authoritative. */
    const rendered = renderedText(readFileSync(join(LEGAL_DIR, name), "utf-8"));
    const bracketed = rendered.match(/\[[A-Z][A-Z \-/]+\]/g) ?? [];
    expect(bracketed.length, `${name} has no placeholders — has it been filled in?`).toBeGreaterThan(0);
  });
});

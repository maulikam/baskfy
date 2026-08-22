/**
 * The marker every legal draft in `src/content/legal/` must open with — Prompt 18 deliverable 3:
 *
 *     "Mark them clearly as DRAFTS REQUIRING LEGAL REVIEW at the top of each file in the repo, not
 *      on the rendered page."
 *
 * An MDX comment (`{/* … *\/}`) compiles to nothing, so the marker is unmissable in the source and
 * absent from the HTML. `src/lib/__tests__/legal-drafts.test.ts` asserts both halves — that every
 * document carries it, and that none of them renders it.
 *
 * Why not on the page: a "DRAFT" watermark on a live terms page is worse than no terms page. It
 * invites a customer to argue that nothing was agreed, which is the opposite of what the document
 * is for. The right place for the warning is in front of the people who can act on it.
 */
export const DRAFT_MARKER = "DRAFT REQUIRING LEGAL REVIEW";

/** The last date the drafts were touched. Shown on every legal page, because an undated policy
 *  cannot be reasoned about — a customer needs to know which version they agreed to. */
export const LEGAL_LAST_UPDATED = "2026-08-21";

export interface LegalDocument {
  slug: string;
  title: string;
  description: string;
}

export const LEGAL_DOCUMENTS: readonly LegalDocument[] = [
  {
    slug: "terms-conditions",
    title: "Terms & Conditions",
    description:
      "The agreement between you and Baskfy: what the service is, what the plans include, what " +
      "Forever means, and the limits of what any of it claims.",
  },
  {
    slug: "privacy-policy",
    title: "Privacy Policy",
    description:
      "What personal data Baskfy collects, why, how long it is kept, and the rights the DPDP Act " +
      "gives you over it.",
  },
  {
    slug: "refund-policy",
    title: "Refund Policy",
    description: "When a Baskfy subscription can be refunded, how to ask, and what happens next.",
  },
  {
    slug: "disclaimer",
    title: "Disclaimer",
    description:
      "Baskfy is not a SEBI-registered investment adviser. What that means for every number on " +
      "the site, at length.",
  },
] as const;

/**
 * Throws rather than returning `undefined`.
 *
 * Every caller is a route module whose own directory name is the slug, so a miss means the route
 * and the registry have gone out of step — which should fail the build, not render a page with no
 * title. It also keeps the call sites free of the non-null assertion that would otherwise be the
 * alternative.
 */
export function legalDocument(slug: string): LegalDocument {
  const found = LEGAL_DOCUMENTS.find((document) => document.slug === slug);
  if (!found) throw new Error(`No legal document registered for "${slug}"`);
  return found;
}

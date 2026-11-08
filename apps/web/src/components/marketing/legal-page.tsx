import type { ReactNode } from "react";

import { formatTradeDate } from "@/lib/format";
import { LEGAL_LAST_UPDATED } from "@/lib/marketing/legal";

/**
 * The frame every legal document renders in.
 *
 * One component rather than four copies, so the "last updated" date and the review notice cannot
 * drift between documents. The date matters: an undated policy cannot be reasoned about, because
 * a customer has no way to know which version they agreed to.
 *
 * The **DRAFT REQUIRING LEGAL REVIEW** marker is deliberately not rendered here — Prompt 18 §3
 * puts it "at the top of each file in the repo, not on the rendered page", and
 * `src/lib/__tests__/legal-drafts.test.ts` asserts both halves.
 */
export function LegalPage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <article className="mx-auto max-w-3xl px-6 py-14">
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
      <p className="mt-2 text-xs text-muted-foreground">
        Last updated{" "}
        <time dateTime={LEGAL_LAST_UPDATED}>{formatTradeDate(LEGAL_LAST_UPDATED)}</time>
      </p>
      <div className="mt-8">{children}</div>
    </article>
  );
}

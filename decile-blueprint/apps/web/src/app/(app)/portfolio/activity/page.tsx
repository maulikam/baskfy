import type { Metadata } from "next";
import Link from "next/link";

import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/portfolio/activity` — the ledger behind Overview and Holdings.
 *
 * PORTFOLIO_REDESIGN.md §7 names what belongs here: buys and sells, internal cash assignments
 * (§4.4), dividends, corporate actions (§4.5), rebalances, and reconciliation history (§4.3).
 * All six are Phase 1 of §10 and none exists yet, so the page is honest about being empty rather
 * than inventing a feed. The route exists now so the §2 tab row is complete.
 */
export const metadata: Metadata = {
  title: PAGES["/portfolio/activity"].title,
  description: PAGES["/portfolio/activity"].blurb,
  robots: { index: false, follow: false },
};

export default function PortfolioActivityPage() {
  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <SectionTabs section="portfolio" />
      <PageHeader title="Activity" blurb={PAGES["/portfolio/activity"].blurb} />

      <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center">
        <p className="max-w-[52ch] text-sm leading-relaxed text-muted-foreground">
          Nothing has happened yet. Once a broker is connected, every buy, sell, dividend and
          cash move it reports is listed here, newest first.
        </p>
        <Link
          href="/brokers"
          className="mt-4 text-sm text-accent underline-offset-4 hover:underline"
        >
          Connect a broker
        </Link>
      </div>

      <p className="text-xs text-muted-foreground">
        A record of what happened, not an instruction to do anything. Nothing here places an order.
      </p>
    </div>
  );
}

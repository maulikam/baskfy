import type { Metadata } from "next";

import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { BacktestCard } from "@/components/twt/backtest-card";
import { fetchBacktest } from "@/lib/twt/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/twt/backtest` — `docs/twt/05` §3.
 *
 * `02` §3.3 makes this page a condition of switching the strategy on, which is why it is a page
 * rather than a table in a document: the thing a person has to read before committing money
 * should be somewhere they can be sent, and somewhere that updates when a fresh run disagrees
 * with the published one.
 *
 * The caveats are rendered **above** the numbers, by the card, as a component. That is house rule
 * 9's second half and `05` §3 restates it by name — a disclaimer at the bottom of a page of
 * results is a disclaimer that has already failed.
 *
 * Read-only, like the rest of this tree.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/twt/backtest"].title,
  description: PAGES["/twt/backtest"].blurb,
};

export default async function TwtBacktestPage() {
  const backtest = await fetchBacktest();

  return (
    <div className="space-y-8">
      <PageHeader title={PAGES["/twt/backtest"].title} blurb={PAGES["/twt/backtest"].blurb} />
      <SectionTabs section="twt" />
      <BacktestCard backtest={backtest} />
    </div>
  );
}

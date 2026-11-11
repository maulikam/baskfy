import type { Metadata } from "next";

import { MbDeskWatchlist } from "@/components/mb/desk-watchlist";
import { MbRanking } from "@/components/mb/ranking";
import { MbWhatThisIs } from "@/components/mb/what-this-is";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { fetchMe } from "@/lib/auth/me";
import { formatTradeDate } from "@/lib/format";
import { fetchMbToday } from "@/lib/mb/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/mb` — the MB sleeve (PM7): the day's ranking and what it is. Read-only: no server action, no
 * order route, no plan (Track C, `docs/portfolio-ml/README.md`). The date is the last scored session;
 * every price is that session's close.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/mb"].title,
  description: PAGES["/mb"].blurb,
};

export default async function MbPage() {
  const [ranked, investable, me] = await Promise.all([
    fetchMbToday({ all: true }),
    fetchMbToday(),
    fetchMe(),
  ]);
  const today = ranked ?? investable;
  const asOf = today?.as_of ?? null;
  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/mb"].title}
        blurb={PAGES["/mb"].blurb}
        meta={
          asOf ? (
            <span data-testid="mb-as-of">
              Closes of {formatTradeDate(asOf)}
            </span>
          ) : null
        }
      />
      <SectionTabs section="mb" />
      <section aria-labelledby="mb-list-heading" className="space-y-3">
        <h2
          id="mb-list-heading"
          className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
        >
          The model's top 50
        </h2>
        <MbRanking names={today?.names ?? []} asOf={asOf} />
      </section>
      {me?.is_staff ? (
        <MbDeskWatchlist names={investable?.names ?? []} asOf={asOf} />
      ) : null}
      <MbWhatThisIs model={today?.model ?? null} />
    </div>
  );
}

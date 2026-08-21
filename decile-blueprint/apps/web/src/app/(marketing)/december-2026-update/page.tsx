import type { Metadata } from "next";

import { PriceChangeTable } from "@/components/marketing/price-change-table";
import Body from "@/content/blog/december-2026-update.mdx";
import { fetchPlanSummary } from "@/lib/marketing/plan-summary";
import { SITE_URL } from "@/lib/site";

/**
 * `/december-2026-update` — docs/01 §1 lists it as the reference product's "Roadmap / pricing-change
 * announcement page", and Prompt 18 §2 asks for "an announcement page pattern like the reference's
 * 'December 2026 update'".
 *
 * ## The pattern, stated
 *
 * An announcement page is a dated MDX document at a **permanent, self-describing URL**, rendered
 * by a route of its own rather than as a blog post. Three properties make it a pattern rather than
 * a one-off:
 *
 * 1. **The URL names the announcement**, so a link in an email or in the app's banner still means
 *    something a year later. `/blog/december-2026-update` would work; `/announcements/latest`
 *    would not.
 * 2. **The banner points at it.** `(app)/layout.tsx` renders `AnnouncementBanner` with a dismissal
 *    cookie keyed on the announcement id; this page is where that banner's action goes.
 * 3. **It never changes retroactively.** A price rise announced here is a record of what was
 *    announced and when. Corrections are appended and dated, not edited in.
 *
 * The next one is a copy of this directory with a new slug, a new MDX file, and a new
 * `ANNOUNCEMENT_ID` in the app layout. That is the whole pattern.
 *
 * **No price is written anywhere in this page or its MDX.** The document takes the price table as
 * a *prop* — MDX compiles to a component, so `{props.priceTable}` is all it takes — and the table
 * is rendered from `GET /plans`. Prompt 13's fourth acceptance criterion ("No price or entitlement
 * is hard-coded in the web app; all read from the API") applies to an announcement *about* prices
 * at least as strongly as to the checkout page, and this way an operator who reprices a plan and
 * forgets this page finds it already correct.
 */
export const metadata: Metadata = {
  title: "December 2026 update",
  description:
    "What changes in December 2026: a longer price history, backtests over real data, the new " +
    "plan prices, and the list of things that still do not work.",
  alternates: { canonical: "/december-2026-update" },
  openGraph: {
    type: "article",
    title: "December 2026 update",
    url: `${SITE_URL}/december-2026-update`,
    publishedTime: "2026-08-20",
  },
};

export default async function DecemberUpdatePage() {
  const plans = await fetchPlanSummary();

  return (
    <article className="mx-auto max-w-3xl px-6 py-14">
      <p className="text-xs text-muted-foreground">
        Announcement · <time dateTime="2026-08-20">20 Aug 2026</time>
      </p>
      <h1 className="mt-3 text-2xl font-semibold tracking-tight">December 2026 update</h1>
      <div className="mt-8">
        <Body priceTable={<PriceChangeTable plans={plans} />} />
      </div>
    </article>
  );
}

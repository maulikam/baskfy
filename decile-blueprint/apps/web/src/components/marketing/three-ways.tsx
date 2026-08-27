import Link from "next/link";

/**
 * The three ways money actually gets put to work here, and the allocation that holds them together.
 *
 * This section exists because the page spent its first year selling one of them. The screener is
 * the third door, not the building: a visitor who wants a manager's basket, or who wants to keep
 * running the holdings they already have, was reading a momentum tool and leaving.
 *
 * ## What is real, and what this may not say
 *
 * The allocation figure below is **illustrative and labelled as such** — it is a worked example of
 * a shape the product supports, not a portfolio anybody holds. The shape itself is real:
 * `portfolio_sleeve` gives every slice its own `capital`, and its `kind` is either `screen` (the
 * names come from a rule) or `manual` ("capital the owner runs themselves, reported so the totals
 * are honest and never allocated" — which is exactly the two by-hand rows in the example).
 *
 * The figure gained a fourth row on 26 Aug 2026: fundamental picks and IPO allotments. It is a
 * `manual` slice like the long-term one, and it is deliberately *not* sold as a screener feature —
 * `factor_registry` exposes `marketcap_cr` and `pe` as columns and `pe` as a sort factor, which is
 * enough to rank on, and nothing like a fundamentals screener. What the figure claims is only that
 * such names can be a slice of their own with their own capital, which is true today.
 *
 * The combined view is `/portfolio/portfolios`: baskets you hold, screen sleeves, and by-hand leftovers
 * each render as a box. Sleeve capital and investment marks stay separate figures — adding them
 * is not a combined NAV. Per-portfolio allocation remains `GET /portfolios/{id}/allocation`.
 */
const WAYS = [
  {
    title: "Subscribe to a manager",
    body: "A SEBI-registered manager publishes a basket with a written thesis and a versioned constituent list. You subscribe, you invest, and the shares are bought into your own demat — nothing is pooled the way a fund pools money. When the manager publishes a new version you see the diff before you apply it.",
    href: "/discover",
    cta: "Browse baskets",
  },
  {
    title: "Build the rule yourself",
    body: "Sixty-four published factors over fourteen lists of stocks. Write a screen, replay it against the market as it actually stood on a past date, read what the run could not know, and keep it as an allocation inside a portfolio if it survives.",
    href: "/build",
    cta: "Open the builder",
  },
  {
    title: "Keep what you already run",
    body: "The holdings you manage by hand do not have to leave. Bring them in as a slice with its own capital so the totals are honest — they are reported and never allocated, and every rebalance elsewhere computes against a true denominator.",
    href: "/portfolio/portfolios",
    cta: "See portfolios",
  },
] as const;

/**
 * An illustrative split. Labelled, because a made-up number on a marketing page is a claim.
 *
 * Four rows, not the first draft's three. The fourth is the one most people actually have and the
 * figure had no room for: names bought on a fundamentals read, and shares from an IPO allotment.
 * Its `kind` is "By hand" — the same source as the long-term row, which is the honest answer,
 * because neither came from a rule. Two rows sharing a source is not a redundancy in the picture;
 * it is the point of it. Each carries its own `capital`, so a rebalance inside one cannot spend
 * the other's, which is exactly what the paragraph underneath claims.
 *
 * The shares still total 100 and the amounts still total a round crore, so a reader can check the
 * arithmetic in their head. `__tests__/three-ways.test.tsx` checks it too, because a figure whose
 * bar does not fill its track is a bug nobody notices in review.
 */
const EXAMPLE = [
  { name: "A manager's basket", amount: "₹35,00,000", share: 35, kind: "Subscribed" },
  { name: "Your momentum rule", amount: "₹5,00,000", share: 5, kind: "Screen" },
  { name: "Fundamental picks and IPOs", amount: "₹10,00,000", share: 10, kind: "By hand" },
  { name: "Long-term holdings", amount: "₹50,00,000", share: 50, kind: "By hand" },
] as const;

/** The bar's four tiers, darkest first, so the order on the bar matches the order in the list. */
const BAR_TONES = ["bg-foreground", "bg-foreground/70", "bg-foreground/45", "bg-foreground/25"];

export function ThreeWays() {
  return (
    <section aria-labelledby="three-ways" className="border-b border-border">
      <div className="mx-auto max-w-6xl px-6 py-20">
        <h2 id="three-ways" className="vaaya-display max-w-[18ch] text-[2.25rem] sm:text-[3rem]">
          Three ways in. One place.
        </h2>
        <p className="mt-5 max-w-[64ch] text-lg leading-relaxed text-muted-foreground">
          Most people do not run one strategy. They hold something a manager picked, something they
          worked out themselves, and something they have owned for years and will not sell. All
          three belong in the same place, with the same arithmetic underneath.
        </p>

        <ul className="mt-14 grid gap-x-10 gap-y-12 md:grid-cols-3">
          {WAYS.map((way) => (
            <li key={way.title} className="flex flex-col">
              <h3 className="text-[20px] font-medium tracking-[-0.01em]">{way.title}</h3>
              <p className="mt-3 flex-1 text-[15px] leading-relaxed text-muted-foreground">
                {way.body}
              </p>
              <Link
                href={way.href}
                className="mt-5 self-start border-b border-foreground/25 pb-0.5 text-[15px] transition-colors hover:border-foreground"
              >
                {way.cta}
              </Link>
            </li>
          ))}
        </ul>

        {/* The allocation, drawn. A worked example is worth more here than another paragraph. */}
        <div className="vaaya-card mt-16 overflow-hidden">
          <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2 border-b border-border px-6 py-4">
            <h3 className="text-[17px] font-medium">One portfolio, four allocations</h3>
            <p className="vaaya-eyebrow">Illustrative — not a real position</p>
          </div>

          <div className="px-6 py-7">
            <div
              data-testid="allocation-bar"
              className="flex h-3 w-full overflow-hidden rounded-full bg-muted"
            >
              {EXAMPLE.map((row, index) => (
                <div
                  key={row.name}
                  style={{ width: `${row.share}%` }}
                  className={BAR_TONES[index]}
                />
              ))}
            </div>

            <dl className="mt-7 grid gap-x-8 gap-y-6 sm:grid-cols-2 lg:grid-cols-4">
              {EXAMPLE.map((row) => (
                <div key={row.name}>
                  <dt className="text-[15px]">{row.name}</dt>
                  <dd className="mt-1 font-mono text-[24px] tabular-nums tracking-tight">
                    {row.amount}
                  </dd>
                  <dd className="vaaya-eyebrow mt-1">{row.kind}</dd>
                </div>
              ))}
            </dl>

            <p className="mt-7 max-w-[70ch] text-[15px] leading-relaxed text-muted-foreground">
              Each slice carries its own capital, so a rebalance inside one of them never quietly
              spends another&rsquo;s. The two you run yourself — the names you bought on a
              fundamentals read or were allotted in an IPO, and the ones you have held for years —
              are reported and never allocated. The totals stay true without handing either over.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}

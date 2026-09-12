import Link from "next/link";
import type { Route } from "next";

import { FloodButton } from "@/components/marketing/flood-button";
import { FAQ_ENTRIES } from "@/lib/marketing/faq";
import { INDEX_LISTS } from "@/lib/marketing/landing-band";
import { FACTOR_FAMILIES } from "@/lib/marketing/factor-families";

/**
 * The landing page's long-form sections, built to the structure of vaaya.ai.
 *
 * Maulik asked for that page's look and feel across the whole homepage: its section order, its
 * light/dark alternation, its spacing, its type sizes and its blocks. So the shapes here are theirs
 * and every fact inside them is Baskfy's — the catalog cards list this product's own universes and
 * factor families, the steps describe this product's own hand-off, and the pricing reads from the
 * billing service.
 *
 * The measurements are the ones read off the live page: a `max-w-6xl` column with `px-5 sm:px-7
 * lg:px-10`, headings at weight 300 with -0.02em tracking, 26px cards, and the dark bands set on
 * `#0a0a0a` with `#141414` panels — those two values now live in `.band-dark` (`globals.css`) as
 * `--background` and `--card`, so the markup below asks for `bg-background`/`bg-card` and gets
 * them.
 */

/* ------------------------------------------------------------------ shared */

/** The page's column. Every section shares it, which is what makes the rhythm read as one page. */
function Column({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={`mx-auto w-full max-w-6xl px-5 sm:px-7 lg:px-10 ${className}`}>{children}</div>
  );
}

/**
 * A dark band.
 *
 * Not `.dark` — that would flip the theme class and take the user's own preference with it. The
 * band is a dark *surface* inside whichever theme the reader chose, so it repoints the palette and
 * nothing else, and the alternation reads the same in both themes.
 *
 * **It used to paint two tokens inline, and 12 Sep 2026 proved two is not enough.** `--muted` fell
 * through to the reader's theme, so the footer's block `<Disclaimer/>` painted the LIGHT `--muted`
 * over near-black and put its text on it at 1.69:1; and the quiet ink tier had no token at all, so
 * it was retyped as `#6f6f6f` — 3.67:1 on a `#141414` panel. Axe caught both on the landing page.
 * The whole palette now lives in `.band-dark` in `globals.css`, where `contrast.test.ts` can parse
 * it, and everything inside a band is written in semantic classes.
 *
 * It forwards the rest of a `<section>`'s props, which is not cosmetic: both callers pass
 * `aria-labelledby`, TypeScript exempts hyphenated JSX attributes from excess-property checking,
 * and so the label was being dropped in silence and neither band was a named region.
 */
function DarkBand({
  children,
  className = "",
  ...rest
}: React.ComponentPropsWithoutRef<"section">) {
  return (
    <section className={`band-dark ${className}`} {...rest}>
      {children}
    </section>
  );
}

/* --------------------------------------------------- the three-step panel */

const STEPS = [
  {
    n: "01",
    title: "Connect your broker",
    body: "Zerodha today, more to follow. Holdings sync in so the totals are real from the first screen you run. Nothing is placed, and the connection can be revoked from your broker as easily as from here.",
  },
  {
    n: "02",
    title: "Choose, or build, a strategy",
    body: "Subscribe to a basket a SEBI-registered manager publishes, or write the rule yourself over sixty-six published factors and replay it against the market as it actually stood on a past date.",
  },
  {
    n: "03",
    title: "Confirm the plan yourself",
    body: "Investing produces a read-only order plan — every buy, every sell, every quantity, and what it costs in brokerage and statutory charges. You confirm it. This site has never placed an order and cannot.",
  },
] as const;

export function StepsPanel() {
  return (
    <section aria-labelledby="steps" className="border-b border-border">
      <Column className="py-20 lg:py-28">
        <div className="grid gap-12 lg:grid-cols-2 lg:gap-16">
          <div>
            <h2 id="steps" className="vaaya-display max-w-[15ch] text-[2.25rem] sm:text-[3rem]">
              From a rule you wrote to shares in your own name.
            </h2>
            <p className="mt-7 max-w-[46ch] text-[15px] leading-relaxed text-muted-foreground">
              A screen becomes a basket, a basket takes its place in one of your portfolios — or in
              one allocation inside one — and the portfolio produces an order plan you confirm at your own
              broker.
            </p>
            <p className="mt-4 max-w-[46ch] text-[15px] leading-relaxed text-muted-foreground">
              Every step is reversible up to the moment you press confirm, and a plan you leave
              alone for thirty minutes expires rather than waiting for you.
            </p>
          </div>

          <ul className="space-y-3">
            {STEPS.map((step) => (
              <li key={step.n} className="band-dark rounded-[26px] p-7">
                <div className="flex items-baseline gap-4">
                  <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-quiet-foreground">
                    // {step.n}
                  </span>
                  <h3 className="text-[17px] font-medium">{step.title}</h3>
                </div>
                <p className="mt-4 text-[15px] leading-relaxed text-muted-foreground">{step.body}</p>
              </li>
            ))}
          </ul>
        </div>
      </Column>
    </section>
  );
}

/* -------------------------------------------------------- the catalog grid */

/** Eight cards, each naming something the product actually publishes. */
const CATALOG: ReadonlyArray<{ title: string; note: string; chips: readonly string[] }> = [
  {
    title: "Lists of stocks",
    note: "Fourteen, point-in-time.",
    chips: INDEX_LISTS.slice(0, 7).map((list) => list.label),
  },
  {
    title: "Factor families",
    note: "Sixty-six factors in all.",
    chips: FACTOR_FAMILIES.map((family) => family.label),
  },
  {
    title: "Windows",
    note: "Calendar, not bar counts.",
    chips: ["1 month", "3 months", "6 months", "9 months", "1 year", "3 years", "5 years"],
  },
  {
    title: "Market surfaces",
    note: "Published nightly.",
    chips: ["Indices", "Breadth", "Regime", "New listings", "Corporate actions"],
  },
  {
    title: "Testing",
    note: "Point-in-time, always.",
    chips: ["Backtest", "Fragility", "Assumptions", "Trade log", "Monthly returns", "Drawdown"],
  },
  {
    title: "Baskets",
    note: "Yours or a manager's.",
    chips: ["Versions", "Constituent diff", "Rebalance", "Minimum amount", "Volatility"],
  },
  {
    title: "Portfolios",
    note: "One portfolio, many allocations.",
    chips: ["Allocations", "Weights", "Drift", "Rebalance tracker", "CSV import", "XIRR"],
  },
  {
    title: "Brokers",
    note: "Your demat, not ours.",
    chips: ["Zerodha Kite", "Holdings sync", "Order plan", "Read-only", "Revocable"],
  },
];

export function CatalogGrid() {
  return (
    <DarkBand aria-labelledby="catalog">
      <Column className="py-20 lg:py-28">
        <h2 id="catalog" className="vaaya-display max-w-[22ch] text-[2.25rem] sm:text-[3rem]">
          Every list, every factor, every index — written down.
        </h2>
        <p className="mt-7 max-w-[62ch] text-[15px] leading-relaxed text-muted-foreground">
          Nothing here is a black box you are asked to trust. Each family carries its algebra, each
          list carries the date its membership was true, and every number traces back to the nightly
          run that produced it.
        </p>

        <ul className="mt-14 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {CATALOG.map((card) => (
            <li key={card.title} className="rounded-[26px] bg-card p-6">
              <div className="flex items-baseline justify-between gap-3">
                <h3 className="text-[17px] font-medium">{card.title}</h3>
                <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-quiet-foreground">
                  {card.chips.length}
                </span>
              </div>
              <p className="mt-1 text-[13px] text-quiet-foreground">{card.note}</p>
              <ul className="mt-5 flex flex-wrap gap-1.5">
                {card.chips.map((chip) => (
                  <li
                    key={chip}
                    className="rounded-full border border-border px-2.5 py-1 text-[12px] text-muted-foreground"
                  >
                    {chip}
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>

        <div className="mt-12">
          <FloodButton href="/build" size="nav" tone="light" hoverLabel="Open the builder">
            Browse everything
          </FloodButton>
        </div>
      </Column>
    </DarkBand>
  );
}

/* ------------------------------------------------------ inspectable / code */

const RESPONSE = `GET /api/v1/screens/exmpl0000001/run?as_of=2026-08-18

{
  "as_of":        "2026-08-18",
  "data_version": 1284,
  "result_count": 268,
  "rows": [
    { "rank": 1, "symbol": "CUPID",
      "sharpe_12m": 12.87, "ret_12m": 745.7 }
  ]
}`;

const ASSUMPTION = `Assumptions attached to every backtest

· Orders decided on the rebalance date's close,
  filled at the NEXT day's open.
· Costs charged on both legs of every fill.
· Share counts are whole; the remainder is cash.
· Prices adjusted for splits and bonuses,
  NOT for cash dividends — every return here
  is a PRICE return.`;

export function Inspectable() {
  return (
    <DarkBand aria-labelledby="inspect" className="border-t border-border">
      <Column className="py-20 lg:py-28">
        <h2 id="inspect" className="vaaya-display max-w-[18ch] text-[2.25rem] sm:text-[3rem]">
          Built to be checked, not believed.
        </h2>
        <p className="mt-7 max-w-[62ch] text-[15px] leading-relaxed text-muted-foreground">
          The same numbers reach the API, the results table and the CSV export, because they are
          rounded once when they are written. Every run states the assumptions it made and the data
          version it read.
        </p>

        <div className="mt-14 grid gap-3 lg:grid-cols-2">
          {[RESPONSE, ASSUMPTION].map((block, index) => (
            <pre
              key={index}
              className="overflow-x-auto rounded-[26px] bg-card p-7 font-mono text-[12px] leading-relaxed text-muted-foreground"
            >
              {block}
            </pre>
          ))}
        </div>

        <ul className="mt-10 flex flex-wrap gap-x-8 gap-y-3 text-[15px]">
          {(
            [
              ["Disclaimer", "/disclaimer"],
              ["How it is computed", "/faq"],
              ["Pricing", "/pricing"],
              ["Support", "/support"],
            ] as ReadonlyArray<readonly [string, Route]>
          ).map(([label, href]) => (
            <li key={label}>
              <Link
                href={href}
                className="border-b border-[#3f3f3f] pb-0.5 transition-colors hover:border-foreground"
              >
                {label}
              </Link>
            </li>
          ))}
        </ul>
      </Column>
    </DarkBand>
  );
}

/* ------------------------------------------------------- the giant wordmark */

/**
 * The oversized line that runs off the edge.
 *
 * Deliberately clipped: the source lets its own name overflow the viewport, and the effect only
 * works if the type is genuinely too big for the column rather than merely large. `aria-hidden`
 * because it is a graphic — the sentence it repeats is already the page's h1.
 */
export function WordmarkBanner() {
  return (
    <section className="overflow-hidden border-b border-border py-16 lg:py-24">
      <p
        aria-hidden="true"
        className="vaaya-display whitespace-nowrap px-5 text-[18vw] leading-[0.85] text-foreground sm:px-7 lg:px-10"
      >
        Every strategy you run
      </p>
    </section>
  );
}

/* ------------------------------------------------------------------- FAQ */

export function FaqSection() {
  const items = FAQ_ENTRIES.slice(0, 6);
  return (
    <section aria-labelledby="faq" className="border-b border-border">
      <Column className="py-20 lg:py-28">
        <div className="grid gap-12 lg:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)] lg:gap-16">
          <div>
            <h2 id="faq" className="vaaya-display max-w-[12ch] text-[2.25rem] sm:text-[3rem]">
              Questions, answered.
            </h2>
            <div className="mt-9">
              <FloodButton href="/faq" size="nav" hoverLabel="Read them all">
                All questions
              </FloodButton>
            </div>
          </div>

          <ul className="space-y-2.5">
            {items.map((item) => (
              <li key={item.id} className="band-dark rounded-[18px]">
                <details className="group">
                  <summary className="flex cursor-pointer list-none items-center justify-between gap-6 px-6 py-4 text-[15px] font-medium [&::-webkit-details-marker]:hidden">
                    {item.question}
                    <span
                      aria-hidden="true"
                      className="shrink-0 text-quiet-foreground transition-transform group-open:rotate-45"
                    >
                      +
                    </span>
                  </summary>
                  <div className="space-y-3 px-6 pb-5 text-[15px] leading-relaxed text-muted-foreground">
                    {item.answer.map((paragraph) => (
                      <p key={paragraph.slice(0, 32)}>{paragraph}</p>
                    ))}
                  </div>
                </details>
              </li>
            ))}
          </ul>
        </div>
      </Column>
    </section>
  );
}

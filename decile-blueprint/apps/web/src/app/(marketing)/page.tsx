import type { Metadata } from "next";
import Image from "next/image";
import Link from "next/link";

import { HowItWorksFlow } from "@/components/marketing/how-it-works-flow";
import { LandingFigures, LandingMarquee } from "@/components/marketing/landing-band";
import { FloodButton } from "@/components/marketing/flood-button";
import { SampleScreenTable } from "@/components/marketing/sample-screen-table";
import {
  CatalogGrid,
  FaqSection,
  Inspectable,
  StepsPanel,
  WordmarkBanner,
} from "@/components/marketing/page-sections";
import { ThreeWays } from "@/components/marketing/three-ways";
import { fetchSampleScreen } from "@/lib/marketing/sample-screen";
import { fetchPlanSummary } from "@/lib/marketing/plan-summary";
import { SITE_DESCRIPTION, SITE_NAME, SITE_TAGLINE, SITE_URL } from "@/lib/site";

/**
 * The marketing landing page — docs/08 §Routes: "`/` | marketing landing (SSG)", and Prompt 18
 * deliverable 1: "what the tool does, the factor families, a live sample screen preview, pricing
 * summary, and the SEBI disclaimer. Distinctive, dense, and honest — not a generic SaaS template."
 *
 * The three words in that sentence are the brief:
 *
 * **Distinctive** — the hero is a real screen run, eight rows of real ranked names, above the
 * fold. Not an illustration of a dashboard, not a gradient.
 *
 * **Dense** — docs/08 §"Design principles" opens with "This is a numbers-dense professional tool,
 * not a marketing site", and a landing page that reads nothing like the product it sells is a
 * promise the product then breaks. What it measures are a table. The prices are a table.
 *
 * **Honest** — docs/14 §Tone: "Copy should never promise outcomes; it should promise **clarity
 * about the data**." Every claim on this page is a claim about *what is computed and published*,
 * and each one is checkable: the formulas are in docs/05, the window lengths in docs/13, the
 * membership rule in docs/09. There is no performance figure anywhere, because we have not
 * measured one and would not advertise it if we had. `src/lib/__tests__/copy-lint.test.ts` is the
 * lint that keeps it that way (Prompt 18's third acceptance criterion).
 *
 * The disclaimer is rendered by `SiteFooter`, which renders the `<Disclaimer/>` component — the
 * "in the footer" half of docs/11 §Compliance.
 */
export const metadata: Metadata = {
  title: `${SITE_NAME} — momentum, ranked`,
  description: SITE_DESCRIPTION,
  alternates: { canonical: "/" },
};



export default async function LandingPage() {
  const [sample, plans] = await Promise.all([fetchSampleScreen(), fetchPlanSummary()]);

  /* docs/08 §"Instrument factsheet" already emits JSON-LD per instrument; this is the site-level
     counterpart Prompt 18 §4 asks for. `WebSite` gives the search box its sitelinks target and
     `Organization` is what carries the "not an adviser" fact into a knowledge panel. */
  const jsonLd = {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "WebSite",
        "@id": `${SITE_URL}/#website`,
        url: SITE_URL,
        name: SITE_NAME,
        description: SITE_DESCRIPTION,
        inLanguage: "en-IN",
        publisher: { "@id": `${SITE_URL}/#organization` },
      },
      {
        "@type": "Organization",
        "@id": `${SITE_URL}/#organization`,
        name: SITE_NAME,
        url: SITE_URL,
        description:
          `${SITE_NAME} publishes momentum rankings for NSE-listed equities. It is not a ` +
          "SEBI-registered investment adviser and publishes no investment advice.",
        areaServed: "IN",
      },
      {
        "@type": "SoftwareApplication",
        name: SITE_NAME,
        applicationCategory: "FinanceApplication",
        operatingSystem: "Web",
        url: SITE_URL,
        offers: plans.map((plan) => ({
          "@type": "Offer",
          name: plan.name,
          price: plan.amountRupees,
          priceCurrency: plan.currency,
          url: `${SITE_URL}/pricing`,
        })),
      },
    ],
  };

  return (
    <>
      <script
        type="application/ld+json"
        // JSON-LD has no non-`dangerously` form. The payload is `JSON.stringify` of an object
        // built here from typed constants — never a raw string, and never user input.
        dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
      />

      {/*
        The hero carries `DESIGN.md`'s display scale rather than the app's: 56px at the top of a
        marketing page is the size the type system was drawn for, and the app's 28px heading looked
        like a settings screen. `[text-wrap:balance]` keeps the three lines even instead of leaving
        two words alone on the last one.
      */}
      {/*
        The hero, to the shape of vaaya.ai: a centred column — small mono eyebrow, light display
        headline, one sentence, an action row — sitting above a full-bleed photograph that runs to
        both edges of the viewport.

        The photograph is **ours**, generated for this page. An earlier attempt borrowed the
        source's staging too literally — same desk, same monitor, same field — and was thrown away:
        a generated near-replica of a distinctive photograph is still a replica. What carries over
        is the register, which is what was actually wanted: one small human, an immense quiet
        space, overcast light, desaturated. The scene is a stepwell, whose stacked tiers happen to
        say something a portfolio of allocations would want said.
      */}
      <section className="pb-14 pt-10 text-center">
        <div className="mx-auto w-full max-w-6xl px-5 sm:px-7 lg:px-10">
          <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
            One place for every strategy
          </p>

          <h1 className="vaaya-display mx-auto mt-6 max-w-[18ch] text-[2.75rem] sm:text-[4rem] lg:text-[4.5rem]">
            {SITE_TAGLINE}
          </h1>

          <p className="mx-auto mt-7 max-w-[64ch] text-[17px] leading-relaxed text-muted-foreground">
            Split one portfolio across a manager&rsquo;s basket, a screen you wrote and tested
            yourself, and the long-term holdings you manage yourself — each with its own capital, each
            tracked on its own. The shares stay in your demat, across whichever brokers you use.
          </p>

          <div className="mt-10 flex flex-wrap items-center justify-center gap-4">
            <FloodButton href="/login" size="nav" hoverLabel="Let&rsquo;s go">
              Get started
            </FloodButton>
            <Link
              href="/market/today"
              className="vaaya-pill flex h-11 items-center px-6 text-[15px] text-foreground/70 transition-colors hover:text-foreground"
            >
              See today&rsquo;s market
            </Link>
          </div>
        </div>
      </section>

      {/*
        Full-bleed, and deliberately not `next/image`: this is one above-the-fold hero at a known
        aspect, so the srcset machinery buys nothing a plain `<img>` with explicit dimensions and
        `fetchPriority` does not, and it costs a client component boundary on an otherwise static
        page. The height is clamped so the figure stays small in frame at every width, which is the
        entire point of the composition.
      */}
      <div className="relative w-full overflow-hidden">
        <Image
          src="/images/hero-stepwell.webp"
          alt="A single person sitting alone on one ledge of an immense ancient Indian stepwell, dwarfed by hundreds of tiers of stone steps receding into shadow."
          width={2400}
          height={1340}
          priority
          sizes="100vw"
          className="h-[42vh] max-h-[620px] w-full object-cover object-center sm:h-[52vh] lg:h-[60vh]"
        />

        {/*
          The pill rows and the ticker ride the foot of the picture rather than following it, which
          is what the source does and what makes the photograph read as a stage rather than a slab.
          Absolutely positioned inside the image's own box, so the picture keeps its full height and
          nothing below it moves.
        */}
        <div className="absolute inset-x-0 bottom-0">
          <LandingMarquee />
        </div>
      </div>

      {/*
        Directly under the photograph, and that placement is the argument the page is making.
        It used to sit six sections down, after the catalog grid, where a reader who had already
        decided the site was another signal shop would never reach it. The one sentence that
        distinguishes this product — *nothing here executes anything, and nothing runs until you
        confirm it at your own broker* — is now the first thing said after the image.
      */}
      <section
        aria-labelledby="how-it-works"
        className="vaaya-surface vaaya-shell border-b border-border"
      >
        <div className="mx-auto max-w-6xl px-6 pt-16">
          <h2 id="how-it-works" className="vaaya-display max-w-[24ch] text-[2.25rem] sm:text-[3rem]">
            From intent to result — nothing runs until you confirm.
          </h2>
          {/*
            Retitled and reworded 27 Aug 2026, with the cost box that left the diagram
            (`FLOW-REFINE-PROMPT.md`, `gates/marketing-flow-responsive.md`). The heading used to
            read "with the cost visible before it runs" and the stage drew a cost card to keep it;
            the card is gone on Maulik's instruction, so the promise goes with it — a heading has
            to be something the picture underneath actually draws.

            The fee itself does not go anywhere: it is stated here, in the numbered "Confirm" step
            under the stage, and in full in `fee-faq.tsx`. What this paragraph still must not say
            is that brokerage and STT are on screen *here* — no plan surface in this app renders
            either. `curated_plans.py` and the investment routers carry no cost field at all, and
            `/portfolio/[id]/costs` is accrued platform fees. Our fee is ours to state and is
            stated; the broker's charges are the broker's, and the broker shows them.
          */}
          <p className="mt-7 max-w-[64ch] text-[15px] leading-relaxed text-muted-foreground">
            Nothing on this site executes anything. Every step is reversible up to the moment you
            confirm, and what reaches your broker is a read-only order plan that expires in thirty
            minutes if you leave it. Our fee rides on it &mdash; 1.5% of a buy, capped at
            &#8377;100, plus GST, and nothing at all on a rebalance or an exit. Brokerage and
            statutory charges are your broker&rsquo;s, shown at the broker on the order you
            confirm yourself.
          </p>
        </div>
        <div className="mt-10 pb-16">
          <HowItWorksFlow />
        </div>
      </section>

      <LandingFigures />


      <ThreeWays />

      <StepsPanel />

      <CatalogGrid />

      <section aria-labelledby="sample" className="mx-auto max-w-6xl px-6 pb-16">
        <h2 id="sample" className="mb-2 text-2xl">
          A search, actually running
        </h2>
        <p className="mb-5 max-w-[62ch] text-base leading-relaxed text-muted-foreground">
          Not a picture of one. This table comes from the same place the application reads, it is
          one of the six ready-made searches, and you can read it without an account.
        </p>
        <SampleScreenTable sample={sample} />
      </section>


      <Inspectable />

      <WordmarkBanner />

      <section aria-labelledby="pricing-summary" className="border-t border-border">
        <div className="mx-auto max-w-6xl px-6 py-16">
          <h2 id="pricing-summary" className="text-2xl">
            What it costs
          </h2>
          {plans.length === 0 ? (
            <p className="mt-3 max-w-prose text-sm text-muted-foreground">
              Prices are served by the billing service, which did not answer when this page was
              built. The{" "}
              <Link className="underline underline-offset-2" href="/pricing">
                pricing page
              </Link>{" "}
              reads them live.
            </p>
          ) : (
            <>
              <ul className="mt-6 grid gap-4 sm:grid-cols-3">
                {plans.map((plan) => (
                  <li key={plan.code} className="rounded-lg border border-border p-4">
                    <h3 className="text-sm font-medium">{plan.name}</h3>
                    <p className="mt-2 text-2xl font-semibold tabular-nums">{plan.display}</p>
                    <p className="mt-1 text-xs text-muted-foreground">{plan.cadence}</p>
                  </li>
                ))}
              </ul>
              <p className="mt-4 max-w-prose text-xs text-muted-foreground">
                Prices are inclusive of GST, and every plan carries the same features. The{" "}
                <Link className="underline underline-offset-2" href="/pricing">
                  pricing page
                </Link>{" "}
                sets out what &ldquo;Forever&rdquo; means before you buy, and the{" "}
                <Link className="underline underline-offset-2" href="/refund-policy">
                  refund policy
                </Link>{" "}
                says what happens if you change your mind.
              </p>
            </>
          )}
        </div>
      </section>

      <FaqSection />
    </>
  );
}

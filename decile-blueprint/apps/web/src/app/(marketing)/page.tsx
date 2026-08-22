import type { Metadata } from "next";
import Link from "next/link";

import { SampleScreenTable } from "@/components/marketing/sample-screen-table";
import { Button } from "@/components/ui/button";
import { fetchSampleScreen } from "@/lib/marketing/sample-screen";
import { fetchPlanSummary } from "@/lib/marketing/plan-summary";
import { FACTOR_FAMILIES } from "@/lib/marketing/factor-families";
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
 * promise the product then breaks. The factor families are a table. The prices are a table.
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

const POINTS = [
  {
    title: "Every formula is written down",
    body: "Sixty-four ranking factors, each with its algebra published in the specification rather than described in a sentence. The Sharpe variants name their window, their annualisation and their zero risk-free rate.",
  },
  {
    title: "Point-in-time index membership",
    body: "A screen run for a past date uses the constituents of that date, not today's. Where membership before 2018 had to be reconstructed, the row says reconstructed rather than pretending otherwise.",
  },
  {
    title: "One rounding, three surfaces",
    body: "Values are rounded once, when they are written. The API, the results table and the CSV export read the same stored number, so a figure cannot change depending on where you read it.",
  },
  {
    title: "Adjusted by default, raw on request",
    body: "Splits, bonuses and dividends are folded into an adjustment factor, so a return is a return and not a corporate action. The exchange print is kept alongside it and is what the price column shows.",
  },
] as const;

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

      <section className="mx-auto max-w-5xl px-6 pb-12 pt-14">
        <p className="text-sm font-medium text-accent">{SITE_NAME}</p>
        <h1 className="mt-3 max-w-3xl text-3xl font-semibold tracking-tight sm:text-4xl">
          {SITE_TAGLINE}
        </h1>
        <p className="mt-4 max-w-prose text-muted-foreground">
          Baskfy ranks every NSE-listed equity by momentum every night, across sixty-four published
          factors and fourteen index universes, and shows you the arithmetic behind each number.
        </p>
        <div className="mt-6 flex flex-wrap gap-3">
          <Button variant="primary" asChild>
            <Link href="/register">Create an account</Link>
          </Button>
          <Button variant="outline" asChild>
            <Link href="/dashboard">Open the indices dashboard</Link>
          </Button>
        </div>
      </section>

      <section aria-labelledby="sample" className="mx-auto max-w-5xl px-6 pb-16">
        <h2 id="sample" className="mb-1 text-lg font-semibold tracking-tight">
          A screen, running
        </h2>
        <p className="mb-4 max-w-prose text-sm text-muted-foreground">
          This table is generated by the same endpoint the application calls. It is one of the six
          example screens, and it is available to read without an account.
        </p>
        <SampleScreenTable sample={sample} />
      </section>

      <section aria-labelledby="families" className="border-y border-border bg-muted/30">
        <div className="mx-auto max-w-5xl px-6 py-12">
          <h2 id="families" className="text-lg font-semibold tracking-tight">
            The factor families
          </h2>
          <p className="mt-1 max-w-prose text-sm text-muted-foreground">
            docs/05 fixes the algebra for each. Every family except the last is computed over five
            calendar windows — one, three, six and nine months, and one year.
          </p>
          <div className="mt-6 overflow-x-auto">
            <table className="w-full min-w-[42rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border text-xs uppercase tracking-wide text-muted-foreground">
                  <th scope="col" className="py-2 pr-4 text-left font-medium">
                    Family
                  </th>
                  <th scope="col" className="py-2 pr-4 text-left font-medium">
                    What it measures
                  </th>
                  <th scope="col" className="py-2 text-right font-medium">
                    Factors
                  </th>
                </tr>
              </thead>
              <tbody>
                {FACTOR_FAMILIES.map((family) => (
                  <tr key={family.key} className="border-b border-border/60 last:border-0">
                    <th scope="row" className="py-3 pr-4 text-left font-medium">
                      {family.label}
                    </th>
                    <td className="py-3 pr-4 text-muted-foreground">{family.description}</td>
                    <td className="py-3 text-right tabular-nums text-muted-foreground">
                      {family.count}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section aria-labelledby="what-it-does" className="mx-auto max-w-5xl px-6 py-12">
        <h2 id="what-it-does" className="text-lg font-semibold tracking-tight">
          What it does, precisely
        </h2>
        <ul className="mt-6 grid gap-6 sm:grid-cols-2">
          {POINTS.map((point) => (
            <li key={point.title} className="space-y-1">
              <h3 className="font-medium">{point.title}</h3>
              <p className="text-sm text-muted-foreground">{point.body}</p>
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="pricing-summary" className="border-t border-border">
        <div className="mx-auto max-w-5xl px-6 py-12">
          <h2 id="pricing-summary" className="text-lg font-semibold tracking-tight">
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
    </>
  );
}

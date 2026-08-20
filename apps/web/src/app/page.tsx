import type { Metadata } from "next";
import Link from "next/link";

import { Disclaimer } from "@/components/data/disclaimer";
import { Button } from "@/components/ui/button";
import { SITE_DESCRIPTION, SITE_NAME, SITE_TAGLINE } from "@/lib/site";

/**
 * The marketing landing page — docs/08 §Routes: "`/` | marketing landing (SSG)".
 *
 * Static by default: it reads nothing per request, so Next renders it at build time. docs/14
 * §Tone governs every word here — "Precise, unhyped, numerate. […] Copy should never promise
 * outcomes; it should promise **clarity about the data**." There is no performance claim on this
 * page, and there is no call to action that implies one.
 */
export const metadata: Metadata = {
  title: `${SITE_NAME} — momentum, ranked`,
  description: SITE_DESCRIPTION,
};

const POINTS = [
  {
    title: "64 ranking factors, all of them published",
    body: "Absolute return, Sharpe return, RSI and beta-adjusted variants across five calendar windows, with the formula for each one written down rather than described.",
  },
  {
    title: "Point-in-time index membership",
    body: "A screen run for a past date uses the index constituents of that date, not today's. Reconstructed periods are labelled as reconstructed.",
  },
  {
    title: "One rounding, three surfaces",
    body: "The API, the table and the CSV export are generated from the same stored values, so a number never changes depending on where you read it.",
  },
] as const;

export default function LandingPage() {
  return (
    /* A real `<main>` landmark: the marketing page has no app shell to provide one, and without
       it a screen-reader user has no way to skip the header. Lighthouse scores it too. */
    <main className="mx-auto flex min-h-dvh max-w-3xl flex-col gap-12 px-6 py-16">
      <header className="space-y-4">
        <p className="text-sm font-medium text-accent">{SITE_NAME}</p>
        <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">{SITE_TAGLINE}</h1>
        <p className="max-w-prose text-muted-foreground">{SITE_DESCRIPTION}</p>
        <div className="flex flex-wrap gap-3 pt-2">
          <Button variant="primary" asChild>
            <Link href="/login">Sign in</Link>
          </Button>
          <Button variant="outline" asChild>
            <Link href="/kitchen-sink">See the interface</Link>
          </Button>
        </div>
      </header>

      <section aria-labelledby="what-it-does" className="space-y-6">
        <h2 id="what-it-does" className="text-lg font-semibold tracking-tight">
          What it does
        </h2>
        <ul className="space-y-6">
          {POINTS.map((point) => (
            <li key={point.title} className="space-y-1">
              <h3 className="font-medium">{point.title}</h3>
              <p className="max-w-prose text-sm text-muted-foreground">{point.body}</p>
            </li>
          ))}
        </ul>
      </section>

      <footer className="mt-auto space-y-4 border-t border-border pt-6">
        <Disclaimer variant="block" />
        <p className="text-xs text-muted-foreground">
          © {new Date().getFullYear()} {SITE_NAME}
        </p>
      </footer>
    </main>
  );
}

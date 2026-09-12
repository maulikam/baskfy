import type { Metadata } from "next";
import Link from "next/link";

import { SITE_NAME } from "@/lib/site";

/**
 * `/about` — docs/01 §1 ("Legal / marketing"), docs/08 §Routes ("SSG").
 *
 * docs/14 §Tone governs this page more than any other: "Precise, unhyped, numerate. The product's
 * credibility comes from showing its work — verified formulas, stated assumptions, honest
 * backtests."
 *
 * So the about page is about *method*, not about a founding story. The three sections are the
 * three things a sceptical reader wants to know before trusting a number: where it came from, how
 * it was checked, and what is still wrong with it.
 */
export const metadata: Metadata = {
  title: "About",
  description:
    "How Baskfy is built: where the market data comes from, how the factor formulas were " +
    "verified against a reference export, and which parts are still incomplete.",
  alternates: { canonical: "/about" },
};

const PRINCIPLES = [
  {
    title: "The specification came first",
    body: "Every formula, every column, every rounding rule and every error response was written down before it was implemented, and the implementation is checked against that document rather than the other way round. Where the two disagree, the document wins and the disagreement is recorded.",
  },
  {
    title: "No look-ahead, asserted rather than promised",
    body: "Anything that refers to a past date reads point-in-time index membership and point-in-time factor rows. The backtest engine goes further: its reader physically cannot return a row dated after the simulated date, and a test proves it rather than a code review vouching for it.",
  },
  {
    title: "Rounded once, at write time",
    body: "Storage precision is the contract. The API, the results table and the CSV export all read the same stored number, so the same figure cannot come out differently in three places.",
  },
  {
    title: "The gaps are published",
    body: "Every place where the data is reconstructed, inferred, or simply missing is labelled in the product — on the page, not in a changelog. A backtest over a period where index membership had to be rebuilt says so in its assumptions panel.",
  },
] as const;

const OPEN = [
  "Fundamentals are not ingested, so the price-to-earnings column and its filter are empty.",
  "Published history starts on the date `/meta/status.data_start_date` reports (currently aligned with the plant's earliest bars). A longer backfill may still be running.",
  "The public API is built but switched off, pending a written data-redistribution opinion.",
  "Backtest Sharpe and Sortino figures are excess over a zero risk-free rate until a Treasury-bill series exists.",
] as const;

export default function AboutPage() {
  return (
    <div className="mx-auto max-w-3xl px-6 py-14">
      <h1 className="text-2xl font-semibold tracking-tight">About {SITE_NAME}</h1>
      <p className="mt-4 max-w-prose text-muted-foreground">
        {SITE_NAME} ranks every NSE-listed equity by momentum and shows the arithmetic. It is a
        measurement instrument for people who already run a rules-based process and want the
        measurement to be right, reproducible, and inspectable.
      </p>
      <p className="mt-4 max-w-prose text-muted-foreground">
        The name is the method: rank a universe, take the top slice. It is also the vocabulary the
        product speaks in — D1 for the top bucket, decile drift for rank movement between runs, and
        a hold band that stops a name being sold the day it slips one place.
      </p>

      <section aria-labelledby="principles" className="mt-12">
        <h2 id="principles" className="text-lg font-semibold tracking-tight">
          How it is built
        </h2>
        <ul className="mt-6 space-y-6">
          {PRINCIPLES.map((principle) => (
            <li key={principle.title} className="space-y-1">
              <h3 className="font-medium">{principle.title}</h3>
              <p className="max-w-prose text-sm text-muted-foreground">{principle.body}</p>
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="open" className="mt-12">
        <h2 id="open" className="text-lg font-semibold tracking-tight">
          What is not finished
        </h2>
        <p className="mt-2 max-w-prose text-sm text-muted-foreground">
          This list is on the about page rather than in a private tracker because a tool that hides
          its limits is asking to be trusted further than it has earned.
        </p>
        <ul className="mt-4 list-disc space-y-2 pl-5 text-sm text-muted-foreground">
          {OPEN.map((item) => (
            <li key={item} className="max-w-prose">
              {item}
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="contact" className="mt-12">
        <h2 id="contact" className="text-lg font-semibold tracking-tight">
          Getting in touch
        </h2>
        <p className="mt-2 max-w-prose text-sm text-muted-foreground">
          Corrections to the data or to the formulas are the most useful thing you can send.{" "}
          <Link className="underline underline-offset-2" href="/support">
            The support page
          </Link>{" "}
          goes to a person, and a reported disagreement between a published figure and the exchange
          is treated as a defect.
        </p>
      </section>
    </div>
  );
}

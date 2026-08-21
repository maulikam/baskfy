import type { Metadata } from "next";

import { FAQ_ENTRIES, FAQ_SECTIONS } from "@/lib/marketing/faq";
import { SITE_URL } from "@/lib/site";

/**
 * `/faq` — docs/01 §1 (Content), docs/08 §Routes ("SSG").
 *
 * Rendered as `<details>` elements rather than as a Radix accordion: this page must ship zero
 * client JavaScript to hold its Lighthouse performance score, and a disclosure widget is one of
 * the few interactions the platform already implements accessibly. Every answer is also present
 * in the DOM when collapsed, which is what makes it findable by in-page search and by a crawler.
 *
 * The `FAQPage` JSON-LD is Prompt 18 deliverable 4's "structured data" on this route. It is
 * generated from the same array the page renders, so the rich result and the page cannot diverge.
 */
export const metadata: Metadata = {
  title: "FAQ",
  description:
    "How Decile computes its momentum rankings, which universes and dates it covers, what the " +
    "sentinel filter values mean, what the paid plans include, and what does not work yet.",
  alternates: { canonical: "/faq" },
};

export default function FaqPage() {
  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    "@id": `${SITE_URL}/faq#faq`,
    mainEntity: FAQ_ENTRIES.map((entry) => ({
      "@type": "Question",
      name: entry.question,
      acceptedAnswer: { "@type": "Answer", text: entry.answer.join(" ") },
    })),
  };

  return (
    <div className="mx-auto max-w-3xl px-6 py-14">
      <script
        type="application/ld+json"
        // JSON-LD has no non-`dangerously` form. The payload is `JSON.stringify` of an object
        // built here from a typed constant — never a raw string, and never user input.
        dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
      />
      <h1 className="text-2xl font-semibold tracking-tight">Frequently asked questions</h1>
      <p className="mt-3 max-w-prose text-sm text-muted-foreground">
        If your question is not here, the{" "}
        <a className="underline underline-offset-2" href="/support">
          support page
        </a>{" "}
        reaches a person.
      </p>

      {FAQ_SECTIONS.map((section) => (
        <section key={section.heading} className="mt-10" aria-labelledby={`s-${section.heading}`}>
          <h2
            id={`s-${section.heading}`}
            className="text-xs font-semibold uppercase tracking-wide text-muted-foreground"
          >
            {section.heading}
          </h2>
          <div className="mt-3 divide-y divide-border border-y border-border">
            {section.entries.map((entry) => (
              <details key={entry.id} id={entry.id} className="group py-4">
                <summary className="cursor-pointer list-none font-medium marker:content-none">
                  <span className="inline-flex w-full items-start justify-between gap-4">
                    {entry.question}
                    <span
                      aria-hidden="true"
                      className="mt-1 shrink-0 text-muted-foreground transition-transform group-open:rotate-45"
                    >
                      +
                    </span>
                  </span>
                </summary>
                <div className="mt-3 space-y-3 text-sm text-muted-foreground">
                  {entry.answer.map((paragraph) => (
                    <p key={paragraph} className="max-w-prose">
                      {paragraph}
                    </p>
                  ))}
                </div>
              </details>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

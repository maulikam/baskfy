import type { Metadata } from "next";
import Link from "next/link";

import { SupportForm } from "@/components/marketing/support-form";

/**
 * `/support` — docs/01 §1 (Content), Prompt 18 §2 ("`/support` with a contact form").
 *
 * The form posts to `POST /api/v1/support`, which docs/07 does not describe; `docs/DECISIONS.md`
 * §18.5 records the addition. It is unauthenticated on purpose — someone who cannot sign in is
 * exactly the person who most needs to reach support.
 */
export const metadata: Metadata = {
  title: "Support",
  description:
    "Report a number that looks wrong, ask about billing, or request a feature. Every message " +
    "reaches a person and every one gets a reply.",
  alternates: { canonical: "/support" },
};

const BEFORE_YOU_WRITE = [
  {
    title: "A figure disagrees with my broker",
    body: "Check whether you are comparing the adjusted close against an exchange print. Decile computes every factor on an adjusted series and shows the exchange print in the price column; the two differ by every corporate action since the split date.",
  },
  {
    title: "A stock is missing from my screen",
    body: "A factor with an incomplete window is null, and a null satisfies no filter — a recent listing is excluded by any one-year condition. Circuit filters also remove names rather than flagging them.",
  },
  {
    title: "The P/E column is empty",
    body: "It is empty for every instrument. Nothing fetches fundamentals yet; the FAQ says so and it is a known gap rather than a fault on your account.",
  },
] as const;

export default function SupportPage() {
  return (
    <div className="mx-auto max-w-3xl px-6 py-14">
      <h1 className="text-2xl font-semibold tracking-tight">Support</h1>
      <p className="mt-3 max-w-prose text-muted-foreground">
        Messages go to a person, not to a queue with a bot on the front of it. A reported
        disagreement between a published figure and the exchange is treated as a defect and
        investigated.
      </p>

      <section aria-labelledby="first" className="mt-10">
        <h2 id="first" className="text-lg font-semibold tracking-tight">
          Three things worth checking first
        </h2>
        <p className="mt-1 max-w-prose text-sm text-muted-foreground">
          Not to put you off writing — these are simply the three that come up most, and the{" "}
          <Link className="underline underline-offset-2" href="/faq">
            FAQ
          </Link>{" "}
          covers each at length.
        </p>
        <ul className="mt-5 space-y-5">
          {BEFORE_YOU_WRITE.map((item) => (
            <li key={item.title} className="space-y-1">
              <h3 className="text-sm font-medium">{item.title}</h3>
              <p className="max-w-prose text-sm text-muted-foreground">{item.body}</p>
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="write" className="mt-12">
        <h2 id="write" className="text-lg font-semibold tracking-tight">
          Write to us
        </h2>
        <p className="mb-6 mt-1 max-w-prose text-sm text-muted-foreground">
          We keep no ticketing system. Your message is emailed to us and a copy is emailed to you,
          and nothing about it is stored on this site.
        </p>
        <SupportForm />
      </section>
    </div>
  );
}

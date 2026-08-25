import Link from "next/link";

import { Disclaimer } from "@/components/data/disclaimer";
import { CONTENT_ROUTES, LEGAL_ROUTES, PRODUCT_ROUTES } from "@/lib/marketing/routes";
import { Wordmark } from "@/components/shell/wordmark";
import { SITE_NAME } from "@/lib/site";

/**
 * The public footer.
 *
 * docs/11 §Compliance asks for the disclaimer "on every analytics surface, **in the footer**, and
 * on checkout"; this is the footer half. It renders the `<Disclaimer/>` *component* rather than
 * repeating its sentence, which is CLAUDE.md house rule 9 ("Disclaimers are components, not
 * footers") applied to the one place the doc actually says the word footer.
 *
 * The link columns are generated from `lib/marketing/routes`, so a new legal page cannot be
 * shipped orphaned.
 */
const COLUMNS = [
  { heading: "Product", routes: PRODUCT_ROUTES },
  { heading: "Company", routes: CONTENT_ROUTES },
  { heading: "Legal", routes: LEGAL_ROUTES },
] as const;

export function SiteFooter() {
  return (
    /*
      Dark, to the shape of vaaya.ai's own footer: three link columns over a rule, the statutory
      lines beneath them, and the wordmark set enormous across the foot of the page, clipped by the
      viewport rather than fitted to it.

      The tokens are painted rather than inherited, so this band stays dark for a reader whose theme
      is light and for one whose theme is dark. `--border` and `--muted-foreground` are re-pointed
      locally so `<Disclaimer/>` — which is a component precisely so it is never re-typed (CLAUDE.md
      house rule 9) — reads correctly against it without knowing it is on a dark ground.
    */
    <footer
      className="relative mt-24 overflow-hidden bg-[#0a0a0a] text-[#f7f7f7]"
      style={{ ["--border" as string]: "#262626", ["--muted-foreground" as string]: "#a6a6a6" }}
    >
      <div className="relative z-10 mx-auto flex w-full max-w-6xl flex-col gap-12 px-5 pb-16 pt-20 sm:px-7 lg:px-10">
        <div className="grid gap-10 sm:grid-cols-2 lg:grid-cols-4">
          <div className="lg:col-span-1">
            <Wordmark />
            <p className="mt-5 max-w-[34ch] text-[15px] leading-relaxed text-[#a6a6a6]">
              One place for every strategy you run on Indian equities — a manager&rsquo;s basket, a
              rule you wrote, or the holdings you keep by hand.
            </p>
          </div>

          {COLUMNS.map((column) => (
            <nav key={column.heading} aria-label={column.heading} className="space-y-4">
              <h2 className="font-mono text-[11px] uppercase tracking-[0.08em] text-[#6f6f6f]">
                {column.heading}
              </h2>
              <ul className="space-y-2.5 text-[15px]">
                {column.routes.map((route) => (
                  <li key={route.href}>
                    <Link className="text-[#a6a6a6] transition-colors hover:text-[#f7f7f7]" href={route.href}>
                      {route.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </nav>
          ))}
        </div>

        <div className="space-y-5 border-t border-[#262626] pt-8">
          <Disclaimer variant="block" />
          <p className="text-[13px] text-[#6f6f6f]">
            © 2026 {SITE_NAME}. Market data is sourced from NSE and from Zerodha Kite under a
            licence for our own use; the site publishes derived analytics, not vendor bars.
          </p>
        </div>
      </div>

      {/*
        The watermark. `aria-hidden` because it is a graphic — the name is already read out by the
        wordmark above it — and clipped by the footer's own `overflow-hidden` so it runs off both
        edges instead of being scaled to fit, which is what makes it read as a watermark at all.
      */}
      <p
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 -bottom-[0.18em] select-none text-center text-[26vw] font-medium leading-[0.75] tracking-[-0.04em] text-[#f7f7f7]/[0.055]"
      >
        {SITE_NAME}
      </p>
    </footer>
  );
}

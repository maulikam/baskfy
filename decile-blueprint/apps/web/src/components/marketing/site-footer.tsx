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
      is light and for one whose theme is dark — `.band-dark` in `globals.css` is where that palette
      now lives, in one place, parsed by `contrast.test.ts`.

      It used to be two tokens set inline here, `--border` and `--muted-foreground`, and the one it
      did NOT set is exactly the one that broke. `<Disclaimer variant="block"/>` — a component
      precisely so it is never re-typed (CLAUDE.md house rule 9) — paints `bg-muted/50`, so it took
      the LIGHT `--muted` at half alpha over near-black and rendered its own text at **1.69:1**.
      That is axe's `aside > p` on the landing page, 12 Sep 2026. A component cannot be expected to
      know it is standing on a dark ground; the ground has to say so, completely.
    */
    <footer className="band-dark relative mt-24 overflow-hidden">
      <div className="relative z-10 mx-auto flex w-full max-w-6xl flex-col gap-12 px-5 pb-16 pt-20 sm:px-7 lg:px-10">
        <div className="grid gap-10 sm:grid-cols-2 lg:grid-cols-4">
          <div className="lg:col-span-1">
            <Wordmark />
            <p className="mt-5 max-w-[34ch] text-[15px] leading-relaxed text-muted-foreground">
              One place for every strategy you run on Indian equities — a manager&rsquo;s basket, a
              rule you wrote, or the holdings you keep by hand.
            </p>
          </div>

          {COLUMNS.map((column) => (
            <nav key={column.heading} aria-label={column.heading} className="space-y-4">
              <h2 className="font-mono text-[11px] uppercase tracking-[0.08em] text-quiet-foreground">
                {column.heading}
              </h2>
              <ul className="space-y-2.5 text-[15px]">
                {column.routes.map((route) => (
                  <li key={route.href}>
                    <Link className="text-muted-foreground transition-colors hover:text-foreground" href={route.href}>
                      {route.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </nav>
          ))}
        </div>

        <div className="space-y-5 border-t border-border pt-8">
          <Disclaimer variant="block" />
          <p className="text-[13px] text-quiet-foreground">
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
        className="pointer-events-none absolute inset-x-0 -bottom-[0.18em] select-none text-center text-[26vw] font-medium leading-[0.75] tracking-[-0.04em] text-foreground/[0.055]"
      >
        {SITE_NAME}
      </p>
    </footer>
  );
}

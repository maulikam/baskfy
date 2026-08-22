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
    <footer className="mt-16 border-t border-border">
      <div className="mx-auto flex max-w-6xl flex-col gap-8 px-6 py-12">
        {/* The mark closes the page as well as opening it — M37. */}
        <Wordmark />
        <div className="grid gap-8 sm:grid-cols-3">
          {COLUMNS.map((column) => (
            <nav key={column.heading} aria-label={column.heading} className="space-y-3">
              <h2 className="eyebrow">
                {column.heading}
              </h2>
              <ul className="space-y-2 text-sm">
                {column.routes.map((route) => (
                  <li key={route.href}>
                    <Link className="text-muted-foreground hover:text-foreground" href={route.href}>
                      {route.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </nav>
          ))}
        </div>
        <Disclaimer variant="block" />
        <p className="text-xs text-muted-foreground">
          © 2026 {SITE_NAME}. Market data is sourced from NSE and from Zerodha Kite under a
          licence for our own use; the site publishes derived analytics, not vendor bars.
        </p>
      </div>
    </footer>
  );
}

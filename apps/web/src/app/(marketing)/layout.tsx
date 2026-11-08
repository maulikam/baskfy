import type { ReactNode } from "react";

import { Providers } from "@/app/providers";
import { SiteFooter } from "@/components/marketing/site-footer";
import { SiteHeader } from "@/components/marketing/site-header";

/**
 * The public frame — docs/08 §Routes: "`/pricing`, `/faq`, `/about`, `/blog/*`, legal | SSG".
 *
 * **Nothing in this subtree reads the request.** No `cookies()`, no `headers()`, no `auth()`. That
 * is what lets Next prerender every page under it at build time, which is Prompt 18's first
 * acceptance criterion. The consequence is that these pages get no CSP nonce, and
 * `src/middleware.ts` serves them a different (static, nonce-free) policy — the trade is argued in
 * `docs/DECISIONS.md` §18.2.
 */
export default function MarketingLayout({ children }: { children: ReactNode }) {
  return (
    <Providers>
      {/*
        `vaaya-surface` carries the ported token set (globals.css) and `vaaya-shell` paints the
        canvas and sets the face, so every page and every existing component under this layout
        inherits the world without a parallel component tree.

        The header is `fixed`, so `main` opens below it rather than under it: 96px is the capsule's
        52px plus its 16px offset plus a rule of air. A page whose first element is full-bleed art
        can reclaim that space itself.
      */}
      <div className="vaaya-surface vaaya-shell flex min-h-dvh flex-col">
        <SiteHeader />
        <main id="main" className="flex-1 pt-24">
          {children}
        </main>
        <SiteFooter />
      </div>
    </Providers>
  );
}

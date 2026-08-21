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
      <div className="flex min-h-dvh flex-col">
        <SiteHeader />
        <main id="main" className="flex-1">
          {children}
        </main>
        <SiteFooter />
      </div>
    </Providers>
  );
}

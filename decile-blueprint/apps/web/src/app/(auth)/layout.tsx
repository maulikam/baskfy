import { headers } from "next/headers";
import type { ReactNode } from "react";

import { Providers } from "@/app/providers";
import { Disclaimer } from "@/components/data/disclaimer";
import { Wordmark } from "@/components/shell/wordmark";

/**
 * The auth group is dynamic by nature — every page under it posts a credential — so it is where
 * the CSP nonce is read, rather than in the root layout. See `src/app/layout.tsx`.
 *
 * M36 gave the column a card to sit on. A form floating in the middle of an empty page has no
 * edge, so on a wide display the eye has nothing to hold it and the whole screen reads as the
 * thing that failed to load. The card is the same one every panel in the app uses, which also
 * makes this the first surface a new visitor meets that already looks like the product.
 */
export default async function AuthLayout({ children }: { children: ReactNode }) {
  const nonce = (await headers()).get("x-nonce") ?? undefined;

  return (
    <Providers nonce={nonce}>
      <div className="flex min-h-dvh flex-col items-center justify-center gap-6 px-6 py-16">
        <Wordmark className="text-base" />
        <div className="w-full max-w-[24rem] rounded-xl border border-border/70 bg-card p-7 shadow-lift">
          {children}
        </div>
        <Disclaimer className="max-w-[24rem]" />
      </div>
    </Providers>
  );
}

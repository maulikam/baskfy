import Link from "next/link";
import { headers } from "next/headers";
import type { ReactNode } from "react";

import { Providers } from "@/app/providers";
import { Disclaimer } from "@/components/data/disclaimer";
import { SITE_NAME } from "@/lib/site";

/**
 * The auth group is dynamic by nature — every page under it posts a credential — so it is where
 * the CSP nonce is read, rather than in the root layout. See `src/app/layout.tsx`.
 */
export default async function AuthLayout({ children }: { children: ReactNode }) {
  const nonce = (await headers()).get("x-nonce") ?? undefined;

  return (
    <Providers nonce={nonce}>
      <div className="mx-auto flex min-h-dvh max-w-sm flex-col justify-center gap-8 px-6 py-16">
        <Link href="/" className="text-sm font-semibold tracking-tight">
          {SITE_NAME}
        </Link>
        {children}
        <Disclaimer />
      </div>
    </Providers>
  );
}

import Link from "next/link";
import type { ReactNode } from "react";

import { Disclaimer } from "@/components/data/disclaimer";
import { SITE_NAME } from "@/lib/site";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="mx-auto flex min-h-dvh max-w-sm flex-col justify-center gap-8 px-6 py-16">
      <Link href="/" className="text-sm font-semibold tracking-tight">
        {SITE_NAME}
      </Link>
      {children}
      <Disclaimer />
    </div>
  );
}

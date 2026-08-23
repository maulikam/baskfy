import type { Metadata } from "next";

import { CreateBasketForm } from "@/components/create/create-basket-form";
import { PageHeader } from "@/components/shell/page-header";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/create` — SC8. Build a PRIVATE basket (≥2 instruments, equal/custom weights).
 * Preview stubbed; save frames visibility=PRIVATE. No execute / OrderGateway.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/create"].title,
  description: PAGES["/create"].blurb,
  robots: { index: false, follow: false },
};

export default function CreateBasketPage() {
  const page = PAGES["/create"];
  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <PageHeader title={page.title} blurb={page.blurb} />
      <CreateBasketForm />
    </div>
  );
}

import type { ReactNode } from "react";

import { BasketSectionNav } from "@/components/basket/basket-section-nav";
import { ShareBasketLink } from "@/components/basket/share-basket-link";

/**
 * Basket segment chrome: section tabs + share control (AF I.1 / I.5).
 */

export default async function BasketSlugLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <BasketSectionNav slug={slug} />
        <ShareBasketLink slug={slug} />
      </div>
      {children}
    </div>
  );
}

import type { ReactNode } from "react";

import { ShareBasketLink } from "@/components/basket/share-basket-link";

/**
 * Basket segment chrome: share control + children (AF I.5). Versions nav lives on the
 * versions page itself; Overview/Constituents still need a Versions tab link (STATUS).
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
      <div className="flex justify-end">
        <ShareBasketLink slug={slug} />
      </div>
      {children}
    </div>
  );
}

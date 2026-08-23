import { BasketCard as SharedBasketCard, type SharedBasketCardModel } from "@/components/basket/basket-card";
import type { ExploreBasketCard } from "@/lib/explore/fetch";

/**
 * Explore catalog adapter → shared BasketCard (Tree 6: one source of truth).
 */
export function BasketCard({ basket }: { basket: ExploreBasketCard }) {
  const metrics = basket.metrics;
  const model: SharedBasketCardModel = {
    name: basket.name,
    thesis:
      basket.description_md?.replace(/[#*_`]/g, "").replace(/\s+/g, " ").trim() ||
      "No one-line summary yet.",
    href: `/basket/${basket.slug}`,
    minAmount: metrics?.min_amount ?? null,
    headlinePct: metrics?.headline_pct ?? null,
    headlineLabel: metrics?.headline_label ?? null,
    badge: basket.access,
    volatility: metrics?.volatility_bucket ?? null,
  };
  return <SharedBasketCard basket={model} />;
}

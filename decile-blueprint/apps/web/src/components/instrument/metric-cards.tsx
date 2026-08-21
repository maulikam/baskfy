import type { MetricCardOut } from "@baskfy/api-client";

import { Sparkline } from "@/components/data/sparkline";
import { StatCard } from "@/components/data/stat-card";
import { formatCell } from "@/lib/instrument/present";

/**
 * docs/01 §5 block 4, rendered per docs/08:
 *
 *     "**Metric cards with medians** — value, sparkline, and a subdued `Median: x` line. The
 *      median is the stock's own history; add a tooltip saying so."
 *
 * The tooltip says more than "own history": it says over how many observations. A median of three
 * rows and a median of three years look identical on a card, and only one of them is worth
 * anything — so the count travels in the payload and is stated here.
 */
export interface MetricCardsProps {
  cards: readonly MetricCardOut[];
  /** Key → the series behind that card's sparkline. Missing or short series draw a flat rule. */
  series: Readonly<Record<string, readonly number[]>>;
}

function medianExplanation(card: MetricCardOut): string {
  if (card.observations === 0) {
    return "There is no history for this metric yet, so no median can be taken.";
  }
  const noun = card.observations === 1 ? "observation" : "observations";
  return `The median of this instrument's own ${card.observations.toLocaleString("en-IN")} ${noun}, not of the universe.`;
}

export function MetricCards({ cards, series }: MetricCardsProps) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
      {cards.map((card) => {
        const values = series[card.key] ?? [];
        return (
          <StatCard
            key={card.key}
            label={card.label}
            value={formatCell(card.key, card.value)}
            median={formatCell(card.key, card.median)}
            medianExplanation={medianExplanation(card)}
            sparkline={<Sparkline values={values} label={`${card.label} history`} />}
          />
        );
      })}
    </div>
  );
}

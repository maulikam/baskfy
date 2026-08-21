import type { MarketQualityOut } from "@baskfy/api-client";

import { MetricGrid } from "@/components/data/metric-grid";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { EMPTY_CELL, formatNumber } from "@/lib/format";
import { formatCell, formatPositiveDays } from "@/lib/instrument/present";

/**
 * docs/01 §5 block 10 — "**Market Quality** — **Wasserstein Regime** (`BULL`), Median Vol 1Y
 * (₹ cr), Circuits 1y/9m/6m/3m/1m, Positive Days 1y/9m/6m/3m/1m."
 *
 * docs/05 §15: "Expose the two distances in the API so the label is explainable rather than
 * magic." They are in the payload, so they are on the page: the badge says BULL, and the tooltip
 * says how far the recent return distribution sits from each reference. A label with no distance
 * behind it is exactly the magic the document is warning about.
 */
export interface MarketQualityProps {
  quality: MarketQualityOut;
  windows: readonly string[];
}

const DISTANCE_DECIMALS = 4;

function regimeVariant(regime: string | null | undefined) {
  if (regime === "BULL") return "positive" as const;
  if (regime === "BEAR") return "negative" as const;
  return "neutral" as const;
}

export function MarketQuality({ quality, windows }: MarketQualityProps) {
  const bull = quality.regime_distance_bull;
  const bear = quality.regime_distance_bear;
  const explanation =
    bull === null || bull === undefined || bear === null || bear === undefined
      ? "There is not enough price history to build the reference distributions this label is derived from, so no distances are shown."
      : `The last 63 daily returns sit a Wasserstein distance of ${formatNumber(bull, { decimals: DISTANCE_DECIMALS })} from this instrument's bullish reference and ${formatNumber(bear, { decimals: DISTANCE_DECIMALS })} from its bearish one. The nearer reference wins.`;

  return (
    <div className="flex flex-col gap-4">
      <dl className="flex flex-wrap items-baseline gap-x-8 gap-y-2">
        <div className="flex items-baseline gap-2">
          <dt className="text-xs text-muted-foreground">Wasserstein Regime</dt>
          <dd>
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="cursor-help">
                  <Badge variant={regimeVariant(quality.regime)}>
                    {quality.regime ?? EMPTY_CELL}
                  </Badge>
                </span>
              </TooltipTrigger>
              <TooltipContent className="max-w-xs">{explanation}</TooltipContent>
            </Tooltip>
          </dd>
        </div>
        <div className="flex items-baseline gap-2">
          <dt className="text-xs text-muted-foreground">Median Vol 1Y</dt>
          <dd className="text-sm font-medium tnum">
            {formatCell("median_vol_12m", quality.median_vol_12m)}
          </dd>
        </div>
      </dl>

      <MetricGrid
        columns={windows}
        caption="Circuit-hit days and the share of positive days across the five windows"
        rows={[
          {
            family: "Circuits",
            cells: quality.circuits.map((cell) => ({
              display: formatCell("circuits_12m", cell.value),
            })),
          },
          {
            family: "Positive days",
            cells: quality.positive_days.map((cell) => ({
              display: formatPositiveDays(cell.value),
            })),
          },
        ]}
      />
    </div>
  );
}

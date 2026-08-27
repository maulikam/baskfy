import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export type VolatilityBucket = "LOW" | "MED" | "HIGH";

const LABELS: Record<VolatilityBucket, string> = {
  LOW: "Low volatility",
  MED: "Medium volatility",
  HIGH: "High volatility",
};

/**
 * The volatility bucket, named by its measure.
 *
 * **It used to say "swing", and it used to be coloured.** Both were wrong in the same way.
 * "Low swing" names nothing a reader can check — what is swinging, over what period, measured
 * how — and the green-for-low, amber-for-high pairing made the product take a position it has no
 * business taking: low volatility is not better, it is calmer, and a reader who wants growth is
 * being quietly told off by a colour. `app/globals.css` reserves colour for meaning that has a
 * direction — green up, red down — and volatility has no direction. The word carries it now, and
 * `docs/DISCOVER-AUDIT.md` C7 is the finding.
 *
 * The bucket alone is a summary: `components/discover/metric-stat.tsx` shows the annualised
 * figure beside it wherever there is room, because "18.2% · Medium" can be checked and "Medium"
 * cannot.
 */
export function VolatilityChip({
  bucket,
  className,
}: {
  bucket: string | null | undefined;
  className?: string;
}) {
  if (bucket !== "LOW" && bucket !== "MED" && bucket !== "HIGH") {
    return (
      <Badge variant="outline" className={cn("font-normal", className)}>
        Volatility not computed
      </Badge>
    );
  }
  return (
    <Badge variant="neutral" className={cn("font-normal", className)} title={bucket}>
      {LABELS[bucket]}
      <span className="sr-only"> ({bucket})</span>
    </Badge>
  );
}

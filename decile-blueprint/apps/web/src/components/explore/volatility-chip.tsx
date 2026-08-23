import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export type VolatilityBucket = "LOW" | "MED" | "HIGH";

const LABELS: Record<VolatilityBucket, string> = {
  LOW: "Low swing",
  MED: "Medium swing",
  HIGH: "High swing",
};

/**
 * Volatility chip — docs/smallcase/05. Colour is paired with the word; never the sole cue.
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
        Swing unknown
      </Badge>
    );
  }
  const variant = bucket === "LOW" ? "positive" : bucket === "HIGH" ? "warning" : "neutral";
  return (
    <Badge variant={variant} className={cn("font-normal", className)} title={bucket}>
      {LABELS[bucket]}
      <span className="sr-only"> ({bucket})</span>
    </Badge>
  );
}

import { Badge } from "@/components/ui/badge";
import type { PortfolioRow } from "@/lib/portfolio/overview";

/**
 * §3's source badge, worded by the API to §9.
 *
 * Baskfy is not SEBI-registered, so third-party content is never "managed", never "advisory" and
 * never a "PMS". `source_badge` arrives already spelled *"Subscribed model by {publisher}"* —
 * composed once on the server, where a test asserts the wording over rendered payloads. This
 * component prints that string; it does not assemble a second version of it in the browser,
 * because the second version is the one that would drift.
 *
 * A subscribed badge is tinted and the three self-built sources are not, so the one category
 * with a regulatory obligation attached is also the one the eye lands on.
 */
export function SourceBadge({
  row,
  className,
}: {
  row: Pick<PortfolioRow, "source" | "source_badge">;
  className?: string;
}) {
  return (
    <Badge
      variant={row.source === "SUBSCRIBED" ? "accent" : "neutral"}
      data-source={row.source}
      data-testid="source-badge"
      title={row.source_badge}
      className={className}
    >
      {row.source_badge}
    </Badge>
  );
}

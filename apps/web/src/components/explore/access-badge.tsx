import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * Free vs fee access — Track B fee collection stays off; FEE still labels honestly.
 */
export function AccessBadge({
  access,
  className,
}: {
  access: string;
  className?: string;
}) {
  const free = access === "FREE";
  return (
    <Badge
      variant={free ? "positive" : "outline"}
      className={cn("font-normal", className)}
    >
      {free ? "Free access" : "Fee based"}
    </Badge>
  );
}

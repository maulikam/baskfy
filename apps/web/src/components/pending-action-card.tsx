import { cn } from "@/lib/utils";

/**
 * Pending-action card for home / investments carousels (SC9).
 * Presentational only — dismiss/resolve call the API from the parent.
 */

export interface PendingActionCardProps {
  type: string;
  title: string;
  body?: string | null;
  className?: string;
}

export function PendingActionCard({
  type,
  title,
  body,
  className,
}: PendingActionCardProps) {
  return (
    <article
      className={cn(
        "rounded-xl border border-border/70 bg-card px-4 py-3 text-sm",
        className,
      )}
      data-pending-type={type}
    >
      <p className="font-medium">{title}</p>
      {body ? <p className="mt-1 text-muted-foreground">{body}</p> : null}
    </article>
  );
}

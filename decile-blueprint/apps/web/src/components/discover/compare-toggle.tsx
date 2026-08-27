"use client";

import { useHasSelection, useSelection } from "@/components/discover/selection-provider";
import { cn } from "@/lib/utils";

/**
 * Add or remove one basket from the comparison.
 *
 * At the cap the control is disabled rather than silently evicting the reader's oldest pick, and
 * it says which basket to remove instead of leaving the reason to be guessed. `title` and the
 * accessible label carry the same sentence, so the explanation is not mouse-only.
 */
export function CompareToggle({
  slug,
  name,
  className,
}: {
  slug: string;
  name: string;
  className?: string;
}) {
  const available = useHasSelection();
  const { isSelected, canToggle, toggle, hydrated, max } = useSelection();
  // No provider means no surface to compare on — home, a manager page. A permanently disabled
  // button there is a promise the page cannot keep, so the control is simply absent.
  if (!available) return null;
  const selected = isSelected(slug);
  const allowed = canToggle(slug);
  const blocked = hydrated && !allowed;
  const reason = blocked
    ? `You can compare ${max} baskets at a time. Remove one to add ${name}.`
    : selected
      ? `Remove ${name} from the comparison`
      : `Add ${name} to the comparison`;

  return (
    <button
      type="button"
      onClick={() => toggle(slug)}
      disabled={!hydrated || blocked}
      aria-pressed={selected}
      aria-label={reason}
      title={reason}
      data-testid="compare-toggle"
      data-selected={selected ? "true" : "false"}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs font-medium transition-colors duration-150",
        selected
          ? "border-foreground/40 bg-foreground/5 text-foreground"
          : "border-border text-muted-foreground hover:border-muted-foreground/50 hover:text-foreground",
        (!hydrated || blocked) && "cursor-not-allowed opacity-50",
        className,
      )}
    >
      <span aria-hidden="true">{selected ? "✓" : "+"}</span>
      Compare
    </button>
  );
}

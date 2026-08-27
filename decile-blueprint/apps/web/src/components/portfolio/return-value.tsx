import {
  describeReturn,
  formatRate,
  toneFor,
  type DisplayReturn,
} from "@/lib/portfolio/overview";
import { cn } from "@/lib/utils";

/**
 * The only way a return percentage reaches the screen.
 *
 * `PORTFOLIO_REDESIGN.md` §11 criterion 3: *"Every displayed return number carries a label
 * stating what it is (TWR / XIRR / since-grouped) and its start date on hover."* A bare
 * percentage is a bug, so there is exactly one component that can render one, and it cannot be
 * called without a {@link DisplayReturn} — which cannot be built without the label the API sent.
 *
 * The label is **visible**, not hidden in the tooltip: a reader has to be able to tell at a
 * glance that the 12% beside a subscribed model and the 12% beside a holding group are two
 * different measurements (§5.2). The start date is in `title`, which is what "on hover" means for
 * a mouse and what a screen reader announces anyway.
 *
 * `isModel` gets its own visible marker for §11 criterion 5. Model and actual are never one
 * figure; when they sit next to each other, the reader must be able to see which is which
 * without reading the column header.
 *
 * When the value is null the em dash appears with the reason attached, never a zero. A
 * fabricated 0.00% cannot be told apart from a real flat year.
 */
export interface ReturnValueProps {
  entry: DisplayReturn;
  /** Print the "why not" sentence under the em dash. Off in a table cell, on where there is room. */
  showReason?: boolean;
  /** `lg` for the hero metrics; the default suits a table cell. */
  size?: "sm" | "lg";
  className?: string;
  "data-testid"?: string;
}

export function ReturnValue({
  entry,
  showReason = false,
  size = "sm",
  className,
  "data-testid": testId = "return-value",
}: ReturnValueProps) {
  return (
    <span
      data-testid={testId}
      data-model={entry.isModel ? "true" : "false"}
      title={describeReturn(entry)}
      className={cn("inline-flex flex-col items-start gap-0.5", className)}
    >
      <span
        className={cn(
          "tabular-nums",
          toneFor(entry.value),
          size === "lg" ? "text-2xl font-semibold" : "text-sm",
        )}
      >
        {formatRate(entry.value)}
      </span>
      <span className="text-[11px] leading-tight text-muted-foreground">
        {entry.isModel ? `${entry.label} · model, not yours` : entry.label}
      </span>
      {showReason && entry.value === null && entry.unavailableReason ? (
        <span className="max-w-[40ch] text-[11px] leading-snug text-muted-foreground">
          {entry.unavailableReason}
        </span>
      ) : null}
    </span>
  );
}

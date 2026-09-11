import { halfSizeLine } from "@/lib/twt/copy";
import type { TwtHalfSize } from "@/lib/twt/fetch";

/**
 * `docs/twt/05` §1.4 — the first-live discipline, visible without opening a settings page.
 *
 * `02` §3.6: the first ten entries this strategy ever takes with money behind them are sized at
 * half. It is a counter rather than a setting because it is a fact about what has already
 * happened, and because a reader who cannot see it will read the first small position as a bug.
 *
 * While trading is switched off the line says so instead of counting down. A counter that ticks
 * when nothing can trade is a number describing an event that has not occurred.
 */
export function HalfSizeCounter({ halfSize }: { halfSize: TwtHalfSize }) {
  return (
    <p
      className="max-w-[80ch] rounded-lg border border-border/60 bg-muted/40 px-4 py-3 text-sm"
      data-testid="twt-half-size"
    >
      {halfSizeLine(halfSize)}
    </p>
  );
}

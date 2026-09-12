import type { Route } from "next";
import Link from "next/link";

import { formatFraction, formatPercent } from "@/lib/format";
import {
  SINGLE_NAME_CAP_PCT,
  type BasketHealth,
} from "@/lib/basket/health";
import { cn } from "@/lib/utils";

/**
 * The numbers a sized preview can already tell you (SB3). Facts, not a score.
 *
 * Missing columns stay off the page rather than rendering as zero — a median of nothing is
 * not 0%, and a visitor who trusts a fabricated Sharpe is worse off than one who sees a gap.
 */
export function BasketHealthPanel({
  health,
  backtestHref,
  className,
}: {
  health: BasketHealth;
  /** When the basket came from a saved screen, a link to Replay that rule. `Route`, because the
      screen's public id reaches it at runtime and `typedRoutes` cannot check a `?screen=` query. */
  backtestHref?: Route | undefined;
  className?: string;
}) {
  if (health.names === 0) return null;

  return (
    <section
      className={cn("flex flex-col gap-3 rounded-xl border border-border/70 bg-card p-4", className)}
      data-testid="basket-health"
      aria-labelledby="basket-health-heading"
    >
      <div>
        <h3 id="basket-health-heading" className="text-sm font-medium">
          What these names look like together
        </h3>
        <p className="mt-1 text-xs text-muted-foreground">
          Read off the sized list and the screen’s own 1-year columns. Not a risk score.
        </p>
      </div>

      <dl className="grid gap-3 sm:grid-cols-3">
        <Stat
          label="Largest name"
          value={
            health.largestSleevePct === null
              ? "—"
              : `${health.largestSleevePct.toFixed(1)}% of stocks`
          }
          hint={`Desk single-name cap is ${SINGLE_NAME_CAP_PCT}% of the stocks allocation.`}
        />
        <Stat
          label="1-yr return, median"
          value={formatPercent(health.medianRet12m)}
          hint="Price return of these names, not of the basket."
        />
        <Stat
          label="Bumpiness, median"
          value={formatFraction(health.medianVol12m)}
          hint="1-year volatility of these names. Higher means a bumpier ride."
        />
      </dl>

      {health.notes.length > 0 ? (
        <ul className="space-y-1.5 text-xs text-muted-foreground">
          {health.notes.map((note) => (
            <li key={note.kind} data-testid={`health-note-${note.kind}`}>
              {note.text}
            </li>
          ))}
        </ul>
      ) : null}

      {backtestHref ? (
        <p className="text-xs">
          <Link
            href={backtestHref}
            className="text-accent underline-offset-4 hover:underline"
          >
            See how this screen would have gone
          </Link>
        </p>
      ) : null}
    </section>
  );
}

function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-sm font-medium tabular-nums">{value}</dd>
      <p className="mt-0.5 text-[11px] leading-snug text-muted-foreground">{hint}</p>
    </div>
  );
}

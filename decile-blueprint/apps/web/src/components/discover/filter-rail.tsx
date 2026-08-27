import Link from "next/link";
import type { Route } from "next";

import { UNCOMPUTED_METRIC_KEYS, uncomputedMetrics } from "@/lib/discover/metrics";
import { definedParams, type ExploreListParams } from "@/lib/explore/fetch";
import { cn } from "@/lib/utils";

/**
 * The filters, in a rail that stays put while the results scroll.
 *
 * **Only filters the catalogue can actually apply.** The brief lists twenty advanced ones —
 * Sharpe, Sortino, maximum drawdown, recovery duration, turnover, sector concentration. None of
 * those figures exists anywhere in this product (`docs/DISCOVER-METRICS-GAP.md`), and a filter
 * control that silently matches everything is worse than no control: it teaches a reader that
 * they have narrowed something when they have not. The rail therefore offers the nine the API
 * documents, and *names* the ones it cannot offer, with why, at the bottom.
 *
 * State is entirely in the URL, so a filtered view is a link — the convention the catalogue
 * already used and the reason the brief's "save this filter combination" is a small step from
 * here rather than a new subsystem.
 */

export interface FilterState extends ExploreListParams {
  view?: string;
}

const AMOUNTS: readonly { label: string; value: string }[] = [
  { label: "Under ₹25,000", value: "25000" },
  { label: "Under ₹1L", value: "100000" },
  { label: "Under ₹3L", value: "300000" },
  { label: "Under ₹5L", value: "500000" },
];

const VOLATILITY: readonly { label: string; value: string }[] = [
  { label: "Low", value: "LOW" },
  { label: "Medium", value: "MEDIUM" },
  { label: "High", value: "HIGH" },
];

const REBALANCE: readonly { label: string; value: string }[] = [
  { label: "Weekly", value: "WEEKLY" },
  { label: "Monthly", value: "MONTHLY" },
  { label: "Quarterly", value: "QUARTERLY" },
];

const ACCESS: readonly { label: string; value: string }[] = [
  { label: "Free", value: "FREE" },
  { label: "Paid", value: "PAID" },
];

const SORTS: readonly { label: string; sort: string; order: string }[] = [
  { label: "Cheapest first", sort: "min_amount", order: "asc" },
  { label: "Calmest first", sort: "volatility", order: "asc" },
  { label: "Strongest return", sort: "headline", order: "desc" },
  { label: "Newest", sort: "launched_at", order: "desc" },
];

export function buildHref(base: string, state: FilterState): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(definedParams(state))) {
    if (value) query.set(key, String(value));
  }
  const encoded = query.toString();
  return encoded ? `${base}?${encoded}` : base;
}

function Chip({
  label,
  active,
  href,
}: {
  label: string;
  active: boolean;
  href: string;
}) {
  return (
    <Link
      href={href as Route}
      aria-current={active ? "true" : undefined}
      data-active={active ? "true" : "false"}
      className={cn(
        "rounded-md border px-2.5 py-1 text-xs font-medium transition-colors duration-150",
        active
          ? "border-accent bg-accent-muted text-accent"
          : "border-border/70 bg-card text-muted-foreground hover:border-muted-foreground/50 hover:text-foreground",
      )}
    >
      {label}
    </Link>
  );
}

function Group({
  legend,
  children,
}: {
  legend: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-2">
      <h3 className="eyebrow">{legend}</h3>
      <div className="flex flex-wrap gap-1.5">{children}</div>
    </div>
  );
}

export function FilterRail({
  base,
  state,
  categories,
  className,
}: {
  /** The path filters apply to — `/discover/all`. */
  base: string;
  state: FilterState;
  /** Strategy tags actually present in the catalogue, so no dead option is offered. */
  categories: readonly string[];
  className?: string;
}) {
  // `definedParams` drops the keys whose value is undefined, which is what
  // `exactOptionalPropertyTypes` requires and what keeps a cleared filter out of the URL.
  const toggle = (key: keyof FilterState, value: string): string =>
    buildHref(base, definedParams({ ...state, [key]: state[key] === value ? undefined : value }));

  const active = Boolean(
    state.max_min_amount ||
      state.volatility ||
      state.access ||
      state.category ||
      state.rebalance_frequency,
  );

  return (
    <aside
      className={cn("space-y-5", className)}
      aria-label="Filters"
      data-testid="filter-rail"
    >
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">Filters</h2>
        {active ? (
          <Link
            href={buildHref(base, definedParams({ view: state.view, sort: state.sort, order: state.order })) as Route}
            data-testid="clear-filters"
            className="text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
          >
            Clear
          </Link>
        ) : null}
      </div>

      <Group legend="Investment amount">
        {AMOUNTS.map((option) => (
          <Chip
            key={option.value}
            label={option.label}
            active={state.max_min_amount === option.value}
            href={toggle("max_min_amount", option.value)}
          />
        ))}
      </Group>

      <Group legend="Volatility">
        {VOLATILITY.map((option) => (
          <Chip
            key={option.value}
            label={option.label}
            active={state.volatility === option.value}
            href={toggle("volatility", option.value)}
          />
        ))}
      </Group>

      {categories.length > 0 ? (
        <Group legend="Strategy">
          {categories.map((category) => (
            <Chip
              key={category}
              label={category.replace(/-/g, " ")}
              active={state.category === category}
              href={toggle("category", category)}
            />
          ))}
        </Group>
      ) : null}

      <Group legend="Rebalance">
        {REBALANCE.map((option) => (
          <Chip
            key={option.value}
            label={option.label}
            active={state.rebalance_frequency === option.value}
            href={toggle("rebalance_frequency", option.value)}
          />
        ))}
      </Group>

      <Group legend="Access">
        {ACCESS.map((option) => (
          <Chip
            key={option.value}
            label={option.label}
            active={state.access === option.value}
            href={toggle("access", option.value)}
          />
        ))}
      </Group>

      <Group legend="Sort">
        {SORTS.map((option) => (
          <Chip
            key={option.sort}
            label={option.label}
            active={state.sort === option.sort && state.order === option.order}
            href={buildHref(base, { ...state, sort: option.sort, order: option.order })}
          />
        ))}
      </Group>

      <details className="text-xs" data-testid="filters-not-yet">
        <summary className="cursor-pointer text-muted-foreground underline-offset-2 hover:underline">
          {UNCOMPUTED_METRIC_KEYS.length} filters this catalogue cannot offer yet
        </summary>
        <p className="mt-2 leading-relaxed text-muted-foreground">
          A control that quietly matches everything is worse than no control. These are not
          computed anywhere in this product yet, so they are named rather than faked:
        </p>
        <ul className="mt-2 space-y-1">
          {uncomputedMetrics().map((metric) => (
            <li key={metric.key} className="text-muted-foreground">
              <span className="text-foreground">{metric.label}</span> — {metric.explain}
            </li>
          ))}
        </ul>
      </details>
    </aside>
  );
}

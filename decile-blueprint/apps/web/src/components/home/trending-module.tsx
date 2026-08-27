import Link from "next/link";

import type { Trending, TrendingList } from "@/lib/home/types";
import { cn } from "@/lib/utils";

/**
 * The Trending module — docs/smallcase/05 §6.1, SC9.
 *
 * The SC9 acceptance criterion is one sentence long and it is the whole design of this
 * component: *"each list labeled with what it actually ranks; no fake 'most invested'."*
 *
 * So `ranks_by` renders under every title, always, and it is not optional styling — a list
 * headed "Moved most this month" with no such line reads as an endorsement, which is exactly
 * what docs/11 §Compliance forbids this product from making.
 *
 * And a list the API withheld renders **as a withheld list**, with the reason in the words the
 * API sent. Hiding them would make an honest refusal indistinguishable from a feature nobody
 * built; showing them empty would be worse. Today, with one basket and one investor, every list
 * is withheld — the module says so out loud rather than rendering a hopeful blank.
 */

export interface TrendingModuleProps {
  trending: Trending;
  className?: string;
}

function ListCard({ list }: { list: TrendingList }) {
  return (
    <article
      data-testid={`trending-${list.key}`}
      className="rounded-xl border border-border/70 bg-card p-4"
    >
      <h3 className="text-sm font-semibold">{list.title}</h3>
      <p className="mt-0.5 text-xs text-muted-foreground">{list.ranks_by}</p>
      <ol className="mt-3 space-y-1.5">
        {list.entries.map((entry) => (
          <li key={entry.basket_slug} className="flex items-baseline gap-2 text-sm">
            <span className="w-4 shrink-0 tabular-nums text-xs text-muted-foreground">
              {entry.rank}
            </span>
            <Link
              href={`/basket/${entry.basket_slug}`}
              className="min-w-0 flex-1 truncate underline-offset-4 hover:underline"
            >
              {entry.basket_name}
            </Link>
            <span className="shrink-0 tabular-nums text-xs text-muted-foreground">
              {entry.metric_display}
            </span>
          </li>
        ))}
      </ol>
      <p className="mt-3 text-xs text-muted-foreground">
        {list.metric_label} · {list.eligible} baskets ranked
        {list.population_based && list.population !== null
          ? ` · ${list.population} people`
          : null}
      </p>
    </article>
  );
}

function WithheldCard({ list }: { list: TrendingList }) {
  return (
    <article
      data-testid={`trending-withheld-${list.key}`}
      data-withheld-reason={list.withheld_reason ?? ""}
      className="rounded-xl border border-dashed border-border bg-card/50 p-4"
    >
      <h3 className="text-sm font-medium text-muted-foreground">{list.title}</h3>
      <p className="mt-0.5 text-xs text-muted-foreground">{list.ranks_by}</p>
      <p className="mt-2 text-xs text-muted-foreground">{list.withheld_note}</p>
    </article>
  );
}

export function TrendingModule({ trending, className }: TrendingModuleProps) {
  const published = trending.items.filter((list) => list.withheld_reason === null);
  const withheld = trending.items.filter((list) => list.withheld_reason !== null);
  const showsAReturn = trending.items.some(
    (list) => list.price_return_caveat && list.withheld_reason === null,
  );

  return (
    <section aria-label="Trending" data-testid="trending-module" className={cn("space-y-3", className)}>
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold">Trending</h2>
        <Link
          href="/discover"
          className="text-xs text-muted-foreground underline-offset-4 hover:underline"
        >
          Browse everything
        </Link>
      </div>

      {trending.items.length === 0 ? (
        <p className="rounded-xl border border-dashed border-border bg-card/50 px-4 py-6 text-center text-sm text-muted-foreground">
          Rankings are not available right now.
        </p>
      ) : null}

      {published.length > 0 ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {published.map((list) => (
            <ListCard key={list.key} list={list} />
          ))}
        </div>
      ) : null}

      {/* A6 / M39.3 — CLAUDE.md: a surface showing a return owes the reader this sentence. */}
      {showsAReturn && trending.return_convention_note ? (
        <p className="text-xs text-muted-foreground">{trending.return_convention_note}</p>
      ) : null}

      {withheld.length > 0 ? (
        <details data-testid="trending-withheld" className="rounded-xl border border-border/70 bg-card/50 p-3">
          <summary className="cursor-pointer text-sm">
            {published.length === 0
              ? `No ranking can be published yet — ${withheld.length} lists, and why`
              : `${withheld.length} more lists, and why they are not published yet`}
          </summary>
          <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {withheld.map((list) => (
              <WithheldCard key={list.key} list={list} />
            ))}
          </div>
          <p className="mt-3 text-xs text-muted-foreground">
            A ranking needs at least {trending.min_entries} baskets with the metric, and a
            popularity ranking needs at least {trending.min_population} people. Below that it
            would be a number this product cannot stand behind.
          </p>
        </details>
      ) : null}
    </section>
  );
}

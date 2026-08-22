import type { Route } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { PageHeader as ShellPageHeader } from "@/components/shell/page-header";
import { TermHint } from "@/components/ui/term";
import type { TermId } from "@/lib/vocabulary";
import { PAGES } from "@/lib/vocabulary";
import { cn } from "@/lib/utils";

/**
 * The shared furniture for the desk surfaces — M26.
 *
 * One set of primitives so the five pages look like one product rather than five ports. The
 * desk's own console grew a different table style per page over a year of Fridays; that is
 * exactly what this replaces.
 *
 * Read-only by construction: nothing in this file renders a button, a form or a control.
 */

export function rupees(value: number | null, options?: { sign?: boolean }): string {
  if (value === null) return "–";
  const sign = options?.sign && value > 0 ? "+" : "";
  const formatted = Math.abs(value).toLocaleString("en-IN", { maximumFractionDigits: 0 });
  return `${value < 0 ? "−" : sign}₹${formatted}`;
}

export function pct(value: number | null, options?: { sign?: boolean }): string {
  if (value === null) return "–";
  const sign = options?.sign && value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

export function price(value: number | null): string {
  return value === null ? "–" : value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/**
 * Green above zero, red below, neutral at exactly zero or unknown.
 *
 * The semantic tokens, not Tailwind's palette. M36 replaced `text-emerald-600 dark:text-red-400`
 * with these: the token pair is the one `contrast.test.ts` verifies at 4.5:1 in both themes and
 * `globals.css` documents as carrying meaning, and a hard-coded emerald bypassed both — it looked
 * right until the palette moved underneath it, which is exactly what happened here.
 */
export function toneOf(value: number | null): string {
  if (value === null || value === 0) return "";
  return value > 0 ? "text-positive" : "text-negative";
}

/**
 * Re-exported from the shell so the desk pages and the screener pages share one page head.
 *
 * They did not, before M36: this file had its own, half a size larger and with the description in
 * a different colour, which is most of why moving between the two halves of the app felt like
 * changing product. `lede` is kept as the prop name the five pages already pass.
 */
export function PageHeader({ title, lede, meta }: { title: string; lede: string; meta?: string }) {
  return <ShellPageHeader title={title} blurb={lede} {...(meta ? { meta } : {})} />;
}

/**
 * One headline figure.
 *
 * The label is sentence case rather than the all-caps it used to be. A row of shouted labels
 * competes with the figures underneath them for the eye, and the figure is the point of the card.
 * `term` attaches the plain-English explanation where the label is a measure rather than a plain
 * word (M36).
 */
export function Stat({
  label,
  value,
  tone,
  hint,
  term,
}: {
  label: string;
  value: string;
  tone?: string;
  hint?: string;
  term?: TermId;
}) {
  return (
    <div className="rounded-xl border border-border/70 bg-card p-4">
      <div className="flex items-center gap-1.5">
        <span className="eyebrow">{label}</span>
        {term ? <TermHint id={term} /> : null}
      </div>
      <div className={cn("mt-1.5 text-2xl font-semibold tabular-nums", tone)}>{value}</div>
      {hint && <div className="mt-1 text-xs leading-relaxed text-muted-foreground">{hint}</div>}
    </div>
  );
}

/**
 * A row of figures that always fills its line.
 *
 * `auto-fit` rather than a fixed column count: the desk pages carry four figures on one page and
 * six on another, and a four-column grid holding six leaves two cards stranded on a second row
 * beside a gap the width of two more. Fitting as many as the width allows means neither page has
 * to know what the other does.
 */
export function StatRow({ children }: { children: ReactNode }) {
  return (
    <section className="grid grid-cols-[repeat(auto-fit,minmax(12rem,1fr))] gap-3">{children}</section>
  );
}

/**
 * The table, as a card.
 *
 * `overflow-x-auto` on the wrapper rather than on the page: a wide table scrolls inside its own
 * frame and the page body never scrolls sideways, which is the rule the interface guidelines are
 * most insistent about and the one a data product breaks most often.
 */
export function Table({
  caption,
  head,
  children,
}: {
  caption: string;
  head: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="overflow-hidden rounded-xl border border-border/70 bg-card">
      <div className="overflow-x-auto">
        <table className="w-full text-sm tabular-nums">
          <caption className="sr-only">{caption}</caption>
          <thead>
            <tr className="border-b border-border bg-muted/50 text-left text-xs font-medium text-muted-foreground">
              {head}
            </tr>
          </thead>
          <tbody>{children}</tbody>
        </table>
      </div>
    </div>
  );
}

export function Th({
  children,
  align,
  term,
}: {
  children: ReactNode;
  align?: "right";
  term?: TermId;
}) {
  return (
    <th scope="col" className={cn("py-2.5 pl-4 pr-3", align === "right" && "text-right")}>
      <span
        className={cn("inline-flex items-center gap-1.5", align === "right" && "flex-row-reverse")}
      >
        {children}
        {term ? <TermHint id={term} /> : null}
      </span>
    </th>
  );
}

export function Td({
  children,
  align,
  tone,
}: {
  children: ReactNode;
  align?: "right";
  tone?: string;
}) {
  // `max-w-0` with `truncate` is what lets a cell shrink inside a table layout; without it a long
  // symbol widens the column and pushes the row into a horizontal scroll nobody asked for.
  return (
    <td
      className={cn("max-w-0 truncate py-2.5 pl-4 pr-3", align === "right" && "text-right", tone)}
    >
      {children}
    </td>
  );
}

export function Row({ children }: { children: ReactNode }) {
  return (
    <tr className="border-b border-border/60 transition-colors duration-150 last:border-0 hover:bg-muted/40">
      {children}
    </tr>
  );
}

/**
 * A note the page wants read. The `--warning` token pair, not a raw amber: same reason as
 * `toneOf` above — the token is the one verified for contrast in both themes.
 */
export function Notice({ children }: { children: ReactNode }) {
  return (
    <p
      role="status"
      className="rounded-xl border border-warning/35 bg-warning-muted p-3.5 text-sm leading-relaxed"
    >
      {children}
    </p>
  );
}

/**
 * Nothing to show, said properly.
 *
 * A heading over a bare sentence reads as a page that failed to load. A framed panel with the
 * sentence centred in it reads as a page that loaded and has nothing in it yet, which is the
 * true statement.
 */
export function Empty({ title, body }: { title: string; body: string }) {
  return (
    <>
      <ShellPageHeader title={title} />
      <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center">
        <p className="max-w-[46ch] text-sm leading-relaxed text-muted-foreground">{body}</p>
      </div>
    </>
  );
}

/**
 * The footer every desk page carries.
 *
 * Two sentences, and both earn their place. The first is the guarantee `read-only.test.ts`
 * asserts is present — a person looking at a position table needs to know it is a record and
 * not a control. The second says where the live broker view is, because these pages show what
 * the database knows and the database is not the broker.
 */
export function ReadOnlyFooter({ live }: { live?: string }) {
  return (
    <div className="flex flex-col gap-1 text-xs text-muted-foreground">
      <p>This page is read-only. Orders are placed from the desk console and nowhere else.</p>
      {live && <p>{live}</p>}
    </div>
  );
}

export function DeskNav({ current }: { current: string }) {
  // `Route` rather than `string`, so `typedRoutes` checks each one: a link to a page that does
  // not exist becomes a compile error instead of a 404 someone finds later. Same reasoning as
  // `lib/nav.ts`'s ReadyNavItem.
  // The labels come from `lib/vocabulary` so this strip and the sidebar cannot disagree — before
  // M36 they did, and a link named "Market stance" that opened a page headed "Risk dial" is the
  // kind of small inconsistency that makes a product feel unfinished.
  const links: readonly Route[] = [
    "/baskets",
    "/performance",
    "/holdings",
    "/tradebook",
    "/regime",
    "/reconcile",
  ] as const;
  return (
    <nav aria-label="Elsewhere on the desk" className="flex flex-wrap items-center gap-2 text-sm">
      <span className="text-muted-foreground">See also</span>
      {links
        .filter((href) => href !== current)
        .map((href) => (
          <Link
            key={href}
            className="rounded-md border border-border/70 bg-card px-2.5 py-1 text-xs font-medium transition-colors duration-150 hover:border-muted-foreground/50 hover:bg-muted"
            href={href}
          >
            {PAGES[href as keyof typeof PAGES].title}
          </Link>
        ))}
    </nav>
  );
}

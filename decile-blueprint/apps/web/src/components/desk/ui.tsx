import type { Route } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

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

/** Green above zero, red below, neutral at exactly zero or unknown. */
export function toneOf(value: number | null): string {
  if (value === null || value === 0) return "";
  return value > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400";
}

export function PageHeader({
  title,
  lede,
  meta,
}: {
  title: string;
  lede: string;
  meta?: string;
}) {
  return (
    <header className="flex flex-col gap-1">
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
      <p className="text-sm text-muted-foreground">{lede}</p>
      {meta && <p className="text-xs text-muted-foreground">{meta}</p>}
    </header>
  );
}

export function Stat({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: string;
  tone?: string;
  hint?: string;
}) {
  return (
    <div className="rounded-lg border p-3">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={`mt-1 text-lg font-semibold tabular-nums ${tone ?? ""}`}>{value}</div>
      {hint && <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div>}
    </div>
  );
}

export function StatRow({ children }: { children: ReactNode }) {
  return <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">{children}</section>;
}

export function Table({ caption, head, children }: { caption: string; head: ReactNode; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm tabular-nums">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
            {head}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function Th({ children, align }: { children: ReactNode; align?: "right" }) {
  return (
    <th scope="col" className={`py-2 pr-3 ${align === "right" ? "text-right" : ""}`}>
      {children}
    </th>
  );
}

export function Td({ children, align, tone }: { children: ReactNode; align?: "right"; tone?: string }) {
  return (
    <td className={`py-2 pr-3 ${align === "right" ? "text-right" : ""} ${tone ?? ""}`}>{children}</td>
  );
}

export function Row({ children }: { children: ReactNode }) {
  return <tr className="border-b last:border-0">{children}</tr>;
}

/** An amber note. Used where a page is telling the reader something it wants them not to miss. */
export function Notice({ children }: { children: ReactNode }) {
  return (
    <p role="status" className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
      {children}
    </p>
  );
}

export function Empty({ title, body }: { title: string; body: string }) {
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
      <p className="text-sm text-muted-foreground">{body}</p>
    </div>
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
  const links: readonly (readonly [Route, string])[] = [
    ["/baskets", "Basket"],
    ["/performance", "Performance"],
    ["/holdings", "Holdings"],
    ["/tradebook", "Trades"],
    ["/regime", "Market stance"],
    ["/reconcile", "Plan vs fills"],
  ] as const;
  return (
    <nav aria-label="Desk" className="flex flex-wrap gap-4 text-sm">
      {links
        .filter(([href]) => href !== current)
        .map(([href, label]) => (
          <Link key={href} className="underline underline-offset-4" href={href}>
            {label}
          </Link>
        ))}
    </nav>
  );
}

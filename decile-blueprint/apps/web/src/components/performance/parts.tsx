import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function Card({ title, meta, children }: { title: ReactNode; meta?: ReactNode; children: ReactNode }) {
  return (
    <section className="overflow-hidden rounded-xl border border-border/70 bg-card">
      <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border/60 px-4 py-3">
        <h2 className="text-sm font-semibold">{title}</h2>
        {meta ? <span className="text-xs text-muted-foreground">{meta}</span> : null}
      </header>
      {children}
    </section>
  );
}

export function Badge({ children, tone }: { children: ReactNode; tone?: "ok" | "warn" | "bad" | "info" }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium",
        tone === "ok" && "bg-positive-muted text-positive",
        tone === "warn" && "bg-warning-muted text-warning",
        tone === "bad" && "bg-negative-muted text-negative",
        tone === "info" && "bg-info-muted text-info",
        !tone && "bg-muted text-muted-foreground",
      )}
    >
      {children}
    </span>
  );
}

"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState, useTransition } from "react";

import type { BasketVersionSummary, VersionDiff } from "@/lib/explore/versions";
import { apiOrigin } from "@/lib/api/config";
import { accessToken } from "@/lib/api/browser";
import { formatNumber } from "@/lib/format";

/**
 * Client diff picker for `/basket/[slug]/versions`.
 *
 * The list of versions is server-rendered; only the selected pair and its diff round-trip from
 * the browser so changing from/to does not re-fetch the whole page.
 */

function pct(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return `${formatNumber(value, { decimals: 2 })}%`;
}

export function VersionDiffPanel({
  slug,
  versions,
  initialFrom,
  initialTo,
}: {
  slug: string;
  versions: BasketVersionSummary[];
  initialFrom?: string | undefined;
  initialTo?: string | undefined;
}) {
  const router = useRouter();
  const ordered = useMemo(
    () => [...versions].sort((a, b) => a.version_no - b.version_no),
    [versions],
  );
  const newest = ordered[ordered.length - 1]?.version_no;
  const previous =
    ordered.length >= 2 ? ordered[ordered.length - 2]?.version_no : ordered[0]?.version_no;

  const [fromNo, setFromNo] = useState<number>(
    Number(initialFrom) || previous || newest || 1,
  );
  const [toNo, setToNo] = useState<number>(Number(initialTo) || newest || 1);
  const [diff, setDiff] = useState<VersionDiff | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  useEffect(() => {
    if (fromNo === toNo || ordered.length < 2) {
      setDiff(null);
      setError(fromNo === toNo ? "Pick two different versions." : null);
      return;
    }
    let cancelled = false;
    startTransition(() => {
      void (async () => {
        try {
          const token = await accessToken();
          const url = `${apiOrigin()}/api/v1/explore/${encodeURIComponent(slug)}/versions/diff?from=${fromNo}&to=${toNo}`;
          const response = await fetch(url, {
            headers: token ? { Authorization: `Bearer ${token}` } : {},
          });
          if (!response.ok) {
            throw new Error(`Diff failed (${response.status})`);
          }
          const body = (await response.json()) as VersionDiff;
          if (!cancelled) {
            setDiff(body);
            setError(null);
            router.replace(
              `/basket/${slug}/versions?from=${fromNo}&to=${toNo}`,
              { scroll: false },
            );
          }
        } catch (err) {
          if (!cancelled) {
            setDiff(null);
            setError(err instanceof Error ? err.message : "Diff unavailable");
          }
        }
      })();
    });
    return () => {
      cancelled = true;
    };
  }, [fromNo, toNo, ordered.length, router, slug]);

  if (ordered.length < 2) {
    return (
      <p
        className="rounded-xl border border-dashed border-border bg-card/50 px-4 py-8 text-center text-sm text-muted-foreground"
        data-testid="version-diff-single"
      >
        Only one version is published. A diff appears here after the next rebalance cut.
      </p>
    );
  }

  return (
    <section className="space-y-4" data-testid="version-diff-panel" aria-label="Version diff">
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm">
          <span className="mb-1 block text-xs text-muted-foreground">From</span>
          <select
            className="rounded-md border border-border bg-card px-2 py-1.5"
            value={fromNo}
            onChange={(event) => setFromNo(Number(event.target.value))}
            data-testid="version-from"
          >
            {ordered.map((version) => (
              <option key={version.version_no} value={version.version_no}>
                v{version.version_no} · {version.effective_date}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs text-muted-foreground">To</span>
          <select
            className="rounded-md border border-border bg-card px-2 py-1.5"
            value={toNo}
            onChange={(event) => setToNo(Number(event.target.value))}
            data-testid="version-to"
          >
            {ordered.map((version) => (
              <option key={version.version_no} value={version.version_no}>
                v{version.version_no} · {version.effective_date}
              </option>
            ))}
          </select>
        </label>
        {pending ? <span className="text-xs text-muted-foreground">Loading…</span> : null}
      </div>

      {error ? <p className="text-sm text-destructive">{error}</p> : null}

      {diff ? (
        <div className="grid gap-4 sm:grid-cols-3">
          <DiffColumn title={`Added (${diff.added.length})`} lines={diff.added} kind="added" />
          <DiffColumn
            title={`Removed (${diff.removed.length})`}
            lines={diff.removed}
            kind="removed"
          />
          <DiffColumn
            title={`Weight changed (${diff.weight_changed.length})`}
            lines={diff.weight_changed}
            kind="changed"
          />
          <p className="sm:col-span-3 text-xs text-muted-foreground">
            {diff.unchanged_count} name{diff.unchanged_count === 1 ? "" : "s"} unchanged between
            v{diff.from_version} and v{diff.to_version}.
          </p>
        </div>
      ) : null}
    </section>
  );
}

function DiffColumn({
  title,
  lines,
  kind,
}: {
  title: string;
  lines: VersionDiff["added"];
  kind: "added" | "removed" | "changed";
}) {
  return (
    <div className="rounded-xl border border-border/70 bg-card p-3">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h3>
      {lines.length === 0 ? (
        <p className="mt-2 text-sm text-muted-foreground">None</p>
      ) : (
        <ul className="mt-2 space-y-1.5 text-sm">
          {lines.map((line) => (
            <li key={`${kind}-${line.symbol}`}>
              <span className="font-medium">{line.symbol}</span>
              {kind === "changed" ? (
                <span className="ml-2 text-muted-foreground">
                  {pct(line.weight_pct_from)} → {pct(line.weight_pct_to)}
                </span>
              ) : (
                <span className="ml-2 text-muted-foreground">
                  {pct(kind === "added" ? line.weight_pct_to : line.weight_pct_from)}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

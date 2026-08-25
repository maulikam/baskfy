"use client";

import type { PortfolioNodeOut, SleeveListOut } from "@baskfy/api-client";
import { Layers, Plus, Scale, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { EmptyState } from "@/components/data/empty-state";
import { ErrorState } from "@/components/data/error-state";
import { BookBoxCard } from "@/components/portfolios/book-box";
import { BookOverall } from "@/components/portfolios/book-overall";
import { PortfolioTree } from "@/components/portfolios/portfolio-tree";
import { UploadCard } from "@/components/portfolios/upload-card";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { composeBook, type BookInvestment } from "@/lib/portfolios/book";
import {
  useCreatePortfolio,
  useDeletePortfolio,
  usePatchPortfolio,
  usePortfolioRollups,
  usePortfolios,
  useSleeveMap,
} from "@/lib/portfolios/queries";
import { forestRows, spanningNodeIds } from "@/lib/portfolios/tree";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/me/portfolios` — one book, many boxes.
 *
 * A named CSV book can be split into sleeves (a rule you wrote, a slice you run by hand). Baskets
 * you marked as invested sit in the same overview, each with the stats that ledger actually has.
 * Rebalance still answers "which symbols changed"; this page answers "how is each box doing".
 *
 * Since migration 0019 a portfolio can sit **inside** another one, so the page opens with the
 * shape — which book is under which, and which broker account each is attributed to — before it
 * gets to the boxes. `initial` is still the roots, so the server component that renders this did
 * not have to change; `orphans` and the deeper levels arrive with the client fetch.
 */
export interface PortfoliosListProps {
  /** The **roots** of the forest. Children hang off them; orphans come in separately. */
  initial: PortfolioNodeOut[] | null;
  error: unknown;
  investments: BookInvestment[];
  initialSleeves: Record<number, SleeveListOut>;
  /** Fragments whose parent is not the caller's. Optional so the server page needs no change. */
  initialOrphans?: PortfolioNodeOut[];
}

export function PortfoliosList({
  initial,
  error,
  investments,
  initialSleeves,
  initialOrphans,
}: PortfoliosListProps) {
  const router = useRouter();
  const portfolios = usePortfolios(
    initial ? { data: initial, orphans: initialOrphans ?? [] } : undefined,
  );
  const create = useCreatePortfolio();
  const remove = useDeletePortfolio();
  const move = usePatchPortfolio();
  const [showUpload, setShowUpload] = useState(false);
  const [newName, setNewName] = useState("");
  const [pendingDelete, setPendingDelete] = useState<PortfolioNodeOut | null>(null);

  const forest = portfolios.data ?? null;
  // Depth-first, parents before children: a book nested under another is a section under it here
  // too, rather than a row that lost its place because the list only ever knew about roots.
  const treeRows = useMemo(() => {
    const walked = forestRows(forest);
    return [...walked.rows, ...walked.orphanRows];
  }, [forest]);
  const rows = useMemo(() => treeRows.map((row) => row.node), [treeRows]);

  const sleeveQueries = useSleeveMap(
    rows.map((row) => row.id),
    initialSleeves,
  );
  // Only the containers that declare no broker account: those are the ones whose row would
  // otherwise have to guess at a single broker, and the roll-up is what turns the guess into "N".
  const rollupQueries = usePortfolioRollups(spanningNodeIds(forest));
  const rollups = useMemo(() => {
    const byId = new Map(
      rollupQueries
        .map((query) => query.data)
        .filter((data) => data !== undefined)
        .map((data) => [data.portfolio_id, data] as const),
    );
    return byId;
  }, [rollupQueries]);

  const book = useMemo(
    () =>
      composeBook({
        portfolios: treeRows.map((row, index) => ({
          portfolio: row.node,
          sleeves: sleeveQueries[index]?.data ?? initialSleeves[row.node.id] ?? null,
          depth: row.depth,
          ancestors: row.ancestors,
          orphan: row.orphan,
        })),
        investments,
      }),
    [treeRows, sleeveQueries, initialSleeves, investments],
  );

  if (error) return <ErrorState error={error} onRetry={() => router.refresh()} />;

  async function createEmpty() {
    const created = await create.mutateAsync({
      name: newName.trim() || `Portfolio ${rows.length + 1}`,
      holdings: [],
    });
    setNewName("");
    router.push(`/portfolios/${created.portfolio.id}/sleeves` as never);
  }

  const empty = rows.length === 0 && investments.length === 0;
  const sleevesPending = sleeveQueries.some((query, index) => {
    const portfolioId = rows[index]?.id;
    if (portfolioId === undefined) return false;
    return query.isLoading && query.data === undefined && initialSleeves[portfolioId] === undefined;
  });

  return (
    <div className="space-y-8">
      <SectionTabs section="me" />
      <PageHeader
        title={PAGES["/me/portfolios"].title}
        blurb={PAGES["/me/portfolios"].blurb}
        actions={
          <Button
            variant={showUpload ? "outline" : "primary"}
            size="sm"
            onClick={() => setShowUpload((open) => !open)}
            data-testid="toggle-upload"
          >
            <Plus aria-hidden="true" />
            {showUpload ? "Close" : "New portfolio"}
          </Button>
        }
        meta="Shares stay in your demat. Each box has its own capital. Nothing on this page places an order."
      />

      {showUpload ? (
        <div className="space-y-4">
          <UploadCard />
          <div className="flex flex-wrap items-end gap-2 rounded-md border border-border p-4">
            <div className="min-w-48 flex-1 space-y-1">
              <label htmlFor="empty-name" className="text-sm font-medium">
                …or start empty and divide it into boxes later
              </label>
              <Input
                id="empty-name"
                value={newName}
                placeholder="Portfolio name"
                onChange={(event) => setNewName(event.target.value)}
              />
            </div>
            <Button
              variant="outline"
              size="sm"
              disabled={create.isPending}
              onClick={() => void createEmpty()}
              data-testid="create-empty"
            >
              Create
            </Button>
          </div>
          {create.error ? <ErrorState error={create.error} /> : null}
        </div>
      ) : null}

      {portfolios.isLoading && initial === null ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((index) => (
            <Skeleton key={index} className="h-40 rounded-xl" />
          ))}
        </div>
      ) : empty ? (
        <EmptyState
          title="No boxes yet"
          reason="Bring in a manager's basket, a rule you wrote, leftover demat names, or mutual funds you run by hand — each in its own box, each with its own capital."
          action={{ label: "Upload a CSV", onClick: () => setShowUpload(true) }}
          icon={<Scale className="size-6" />}
        />
      ) : (
        <>
          <PortfolioTree
            forest={forest}
            rollups={rollups}
            moving={move.isPending}
            onMove={(request) => {
              // `parentId: null` is sent as an explicit null, which is what promotes a portfolio
              // to a root; omitting the field would mean "leave the parent alone" instead.
              move.mutate({ id: request.id, parentId: request.parentId });
            }}
          />
          {move.error ? <ErrorState error={move.error} /> : null}
          <BookOverall totals={book.totals} />
          {sleevesPending ? (
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {[0, 1].map((index) => (
                <Skeleton key={index} className="h-40 rounded-xl" />
              ))}
            </div>
          ) : null}
          <ul className="space-y-8" data-testid="portfolio-list">
            {book.sections.map((section) => (
              <li
                key={section.id}
                className="space-y-3"
                data-testid="book-section"
                data-depth={section.depth}
                style={section.depth > 0 ? { marginLeft: `${section.depth * 1.25}rem` } : undefined}
              >
                <div className="flex flex-wrap items-end justify-between gap-2">
                  <div>
                    <h2 className="text-sm font-semibold">{section.title}</h2>
                    {section.ancestors.length > 0 ? (
                      <p className="text-xs text-muted-foreground">
                        inside {section.ancestors.join(" › ")}
                      </p>
                    ) : null}
                    {section.portfolioId !== undefined ? (
                      <p className="text-xs text-muted-foreground">
                        {section.holdingsCount} holding
                        {section.holdingsCount === 1 ? "" : "s"} filed in this book itself
                      </p>
                    ) : (
                      <p className="text-xs text-muted-foreground">
                        Marked as invested — live marks, when the ledger has them
                      </p>
                    )}
                  </div>
                  {section.portfolioId !== undefined ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <Button variant="outline" size="sm" asChild>
                        <Link href={`/portfolios/${section.portfolioId}/rebalance` as never}>
                          <Scale aria-hidden="true" />
                          Rebalance
                        </Link>
                      </Button>
                      <Button variant="outline" size="sm" asChild>
                        <Link href={`/portfolios/${section.portfolioId}/sleeves` as never}>
                          <Layers aria-hidden="true" />
                          Divide
                        </Link>
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Delete ${section.title}`}
                        onClick={() => {
                          const match = rows.find((row) => row.id === section.portfolioId);
                          if (match) setPendingDelete(match);
                        }}
                      >
                        <Trash2 aria-hidden="true" />
                      </Button>
                    </div>
                  ) : (
                    <Button variant="outline" size="sm" asChild>
                      <Link href="/me/investments">Investments</Link>
                    </Button>
                  )}
                </div>
                {section.boxes.length === 0 ? (
                  <p className="rounded-xl border border-dashed border-border bg-card/50 px-4 py-6 text-sm text-muted-foreground">
                    No boxes in this book yet. Divide it into a rule you wrote and what you run by
                    hand, or upload the names you already hold.
                  </p>
                ) : (
                  <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                    {section.boxes.map((box) => (
                      <BookBoxCard key={box.id} box={box} />
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      {remove.error ? <ErrorState error={remove.error} /> : null}

      <Dialog
        open={pendingDelete !== null}
        onOpenChange={(open) => (open ? undefined : setPendingDelete(null))}
      >
        <DialogContent className="max-w-md">
          <DialogTitle className="text-base font-semibold">
            Delete “{pendingDelete?.name}”?
          </DialogTitle>
          <DialogDescription className="mt-2 text-sm text-muted-foreground">
            This removes the named book, its holdings and every rebalance you were shown for it.
            Baskets you hold are a separate ledger and stay.
          </DialogDescription>
          <div className="mt-4 flex gap-2">
            <Button
              variant="destructive"
              size="sm"
              disabled={remove.isPending}
              data-testid="confirm-delete-portfolio"
              onClick={() => {
                if (pendingDelete) {
                  void remove.mutateAsync(pendingDelete.id).then(() => {
                    setPendingDelete(null);
                    router.refresh();
                  });
                }
              }}
            >
              Delete portfolio
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setPendingDelete(null)}>
              Keep it
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

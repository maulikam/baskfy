"use client";

import type { PortfolioSummaryOut } from "@baskfy/api-client";
import { Layers, Plus, Scale, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Disclaimer } from "@/components/data/disclaimer";
import { EmptyState } from "@/components/data/empty-state";
import { ErrorState } from "@/components/data/error-state";
import { UploadCard } from "@/components/portfolios/upload-card";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useCreatePortfolio,
  useDeletePortfolio,
  usePortfolios,
} from "@/lib/portfolios/queries";

/**
 * `/portfolios` — docs/08 §Routes marks it client-rendered, and it is step 1 of the wizard:
 * "choose portfolio (or upload CSV, with a downloadable sample)".
 *
 * A portfolio here is a saved thing, not a one-off upload. docs/01 §10 lists that as the gap we
 * close: "Rebalance tracker is a one-off CSV diff → First-class portfolios with saved holdings +
 * history."
 */
export interface PortfoliosListProps {
  initial: PortfolioSummaryOut[] | null;
  error: unknown;
}

export function PortfoliosList({ initial, error }: PortfoliosListProps) {
  const router = useRouter();
  const portfolios = usePortfolios(initial ?? undefined);
  const create = useCreatePortfolio();
  const remove = useDeletePortfolio();
  const [showUpload, setShowUpload] = useState(false);
  const [newName, setNewName] = useState("");
  const [pendingDelete, setPendingDelete] = useState<PortfolioSummaryOut | null>(null);

  if (error) return <ErrorState error={error} onRetry={() => router.refresh()} />;

  const rows = portfolios.data ?? [];

  async function createEmpty() {
    const created = await create.mutateAsync({
      name: newName.trim() || `Portfolio ${rows.length + 1}`,
      holdings: [],
    });
    setNewName("");
    router.push(`/portfolios/${created.portfolio.id}/rebalance` as never);
  }

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Rebalance Tracker</h1>
          <p className="text-sm text-muted-foreground">
            Keep a portfolio here, then diff it against any screen with a hold buffer.
          </p>
        </div>
        <Button
          variant={showUpload ? "secondary" : "primary"}
          size="sm"
          className="ml-auto"
          onClick={() => setShowUpload((open) => !open)}
          data-testid="toggle-upload"
        >
          <Plus aria-hidden="true" />
          {showUpload ? "Close" : "New portfolio"}
        </Button>
      </header>

      {showUpload ? (
        <div className="space-y-4">
          <UploadCard />
          <div className="flex flex-wrap items-end gap-2 rounded-md border border-border p-4">
            <div className="min-w-48 flex-1 space-y-1">
              <label htmlFor="empty-name" className="text-sm font-medium">
                …or start empty and add holdings later
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
            <Skeleton key={index} className="h-28 rounded-md" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          title="No portfolios yet"
          reason="Upload a CSV of the symbols you hold — a sample is one click away — and the tracker will tell you what to buy, hold and sell against any screen."
          action={{ label: "Upload a CSV", onClick: () => setShowUpload(true) }}
          icon={<Scale className="size-6" />}
        />
      ) : (
        <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" data-testid="portfolio-list">
          {rows.map((portfolio) => (
            <li
              key={portfolio.id}
              className="flex flex-col gap-2 rounded-md border border-border p-4"
            >
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-semibold">{portfolio.name}</p>
                  <p className="text-xs text-muted-foreground">
                    {portfolio.holdings_count} holding{portfolio.holdings_count === 1 ? "" : "s"}
                  </p>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Delete ${portfolio.name}`}
                  onClick={() => setPendingDelete(portfolio)}
                >
                  <Trash2 aria-hidden="true" />
                </Button>
              </div>
              {/*
                Two questions, two surfaces. Rebalance answers "which symbols changed"; Sleeves
                answers "how much goes where" for a portfolio run as several screens (M34).
              */}
              <div className="mt-auto flex gap-2">
                <Button variant="outline" size="sm" asChild className="flex-1">
                  <Link href={`/portfolios/${portfolio.id}/rebalance` as never}>
                    <Scale aria-hidden="true" />
                    Rebalance
                  </Link>
                </Button>
                <Button variant="outline" size="sm" asChild className="flex-1">
                  <Link href={`/portfolios/${portfolio.id}/sleeves` as never}>
                    <Layers aria-hidden="true" />
                    Sleeves
                  </Link>
                </Button>
              </div>
            </li>
          ))}
        </ul>
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
            This removes the portfolio, its holdings and every rebalance you were shown for it.
          </DialogDescription>
          <div className="mt-4 flex gap-2">
            <Button
              variant="destructive"
              size="sm"
              disabled={remove.isPending}
              data-testid="confirm-delete-portfolio"
              onClick={() => {
                if (pendingDelete) {
                  void remove.mutateAsync(pendingDelete.id).then(() => setPendingDelete(null));
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

      <Disclaimer />
    </div>
  );
}

"use client";

import type { PortfolioOut, RebalanceOut } from "@baskfy/api-client";
import { History, Play } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Disclaimer } from "@/components/data/disclaimer";
import { ErrorState } from "@/components/data/error-state";
import { BufferExplainer } from "@/components/portfolios/buffer-explainer";
import { ResultColumn } from "@/components/portfolios/result-column";
import { UploadCard } from "@/components/portfolios/upload-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  usePortfolio,
  useRebalance,
  useRebalanceHistory,
  useScreens,
} from "@/lib/portfolios/queries";

/**
 * `/portfolios/[id]/rebalance` — docs/08 §"Rebalance tracker":
 *
 *     "Wizard: choose portfolio (or upload CSV, with a downloadable sample) → choose screen → set
 *      `top_n` and `hold_buffer` → results as three columns (Exits / Inside WRH / Entries) each
 *      with copy-to-clipboard and CSV. Show unmatched symbols prominently rather than silently
 *      dropping."
 *
 * Step 1 happened on `/portfolios`; this page shows which portfolio is loaded (and lets a user
 * replace its holdings from a file without leaving), then takes the screen and the two rule
 * inputs, then renders the three columns in the reference product's order.
 *
 * Nothing here decides anything. `top_n` and `hold_buffer` go to the server, and the three lists
 * come back computed — the rule lives in `baskfy_core.rebalance` and has exactly one
 * implementation.
 */

const DEFAULT_TOP_N = 20;
const DEFAULT_HOLD_BUFFER = 10;

export interface RebalanceWizardProps {
  portfolioId: number;
  initial: PortfolioOut | null;
}

export function RebalanceWizard({ portfolioId, initial }: RebalanceWizardProps) {
  const portfolio = usePortfolio(portfolioId, initial ?? undefined);
  const screens = useScreens();
  const history = useRebalanceHistory(portfolioId);
  const rebalance = useRebalance();

  const [screenId, setScreenId] = useState("");
  // Kept as the typed string, not a number: a controlled numeric input that snaps back to its
  // default the moment the field is empty cannot be edited — clearing "20" to type "5" gives
  // "205". The parsed value is derived below.
  const [topNText, setTopNText] = useState(String(DEFAULT_TOP_N));
  const [holdBufferText, setHoldBufferText] = useState(String(DEFAULT_HOLD_BUFFER));
  const [result, setResult] = useState<RebalanceOut | null>(null);
  const [showUpload, setShowUpload] = useState(false);

  const topN = parsePositive(topNText, 1, DEFAULT_TOP_N);
  const holdBuffer = parsePositive(holdBufferText, 0, DEFAULT_HOLD_BUFFER);
  const chosenScreen = screenId || screens.data?.[0]?.public_id || "";
  const holdings = portfolio.data?.holdings ?? [];

  async function run() {
    const computed = await rebalance.mutateAsync({
      portfolioId,
      screenPublicId: chosenScreen,
      topN,
      holdBuffer,
    });
    setResult(computed);
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            {portfolio.data?.name ?? "Rebalance"}
          </h1>
          <p className="text-sm text-muted-foreground">
            {holdings.length} holding{holdings.length === 1 ? "" : "s"} · diffed against a screen
            with a hold buffer
          </p>
        </div>
        <Button variant="ghost" size="sm" className="ml-auto" asChild>
          <Link href="/portfolios">All portfolios</Link>
        </Button>
        <Button variant="outline" size="sm" onClick={() => setShowUpload((open) => !open)}>
          {showUpload ? "Close" : "Replace holdings"}
        </Button>
      </header>

      {showUpload ? (
        <UploadCard portfolioId={portfolioId} onImported={() => void portfolio.refetch()} />
      ) : null}

      {portfolio.error ? <ErrorState error={portfolio.error} /> : null}

      <section
        aria-labelledby="rebalance-inputs"
        className="flex flex-wrap items-end gap-3 rounded-md border border-border p-4"
      >
        <h2 id="rebalance-inputs" className="sr-only">
          Screen and buffer
        </h2>

        <div className="min-w-56 flex-1 space-y-1">
          <Label htmlFor="screen">Screen</Label>
          {screens.isLoading ? (
            <Skeleton className="h-9 rounded-md" />
          ) : (
            <Select
              id="screen"
              value={chosenScreen}
              data-testid="screen-select"
              onChange={(event) => setScreenId(event.target.value)}
            >
              {(screens.data ?? []).map((screen) => (
                <option key={screen.public_id} value={screen.public_id}>
                  {screen.name}
                  {screen.is_example ? " (example)" : ""}
                </option>
              ))}
            </Select>
          )}
        </div>

        <div className="w-28 space-y-1">
          <Label htmlFor="top-n">Top N</Label>
          <Input
            id="top-n"
            type="number"
            min={1}
            value={topNText}
            data-testid="top-n"
            onChange={(event) => setTopNText(event.target.value)}
          />
        </div>

        <div className="w-32 space-y-1">
          <Label htmlFor="hold-buffer">Hold buffer</Label>
          <Input
            id="hold-buffer"
            type="number"
            min={0}
            value={holdBufferText}
            data-testid="hold-buffer"
            onChange={(event) => setHoldBufferText(event.target.value)}
          />
        </div>

        <Button
          variant="primary"
          size="sm"
          disabled={!chosenScreen || rebalance.isPending}
          onClick={() => void run()}
          data-testid="run-rebalance"
        >
          <Play aria-hidden="true" />
          {rebalance.isPending ? "Computing…" : "Compute rebalance"}
        </Button>
      </section>

      {rebalance.error ? <ErrorState error={rebalance.error} /> : null}

      <BufferExplainer topN={topN} holdBuffer={holdBuffer} result={result} />

      {result ? (
        <>
          <div className="grid gap-4 lg:grid-cols-3" data-testid="rebalance-results">
            <ResultColumn
              slug="exits"
              title="Exits"
              tone="negative"
              description={`Held, and ranked worse than ${result.top_n + result.hold_buffer} — or gone from the screen.`}
              emptyMessage="Nothing to sell. Every holding is still inside the buffer."
              rows={result.exits}
              asOf={result.as_of}
            />
            <ResultColumn
              slug="inside-wrh"
              title="Inside WRH"
              tone="neutral"
              description={`Held, ranked ${result.top_n + 1}–${result.top_n + result.hold_buffer}. Inside the hold band, so keep them.`}
              emptyMessage="No holding is in the buffer band right now."
              rows={result.inside_wrh}
              asOf={result.as_of}
            />
            <ResultColumn
              slug="entries"
              title="Entries"
              tone="positive"
              description={`In the screen's top ${result.top_n}, not currently held.`}
              emptyMessage="Nothing to buy — you already hold the whole top N."
              rows={result.entries}
              asOf={result.as_of}
            />
          </div>

          <TargetWeights result={result} />
        </>
      ) : null}

      <RebalanceHistory
        portfolioId={portfolioId}
        rows={history.data?.data ?? []}
        loading={history.isLoading}
      />

      <Disclaimer />
    </div>
  );
}

/** The typed text as a number the API will accept; an empty or nonsense field means the default. */
function parsePositive(raw: string, min: number, fallback: number): number {
  const parsed = Number.parseInt(raw, 10);
  if (Number.isNaN(parsed)) return fallback;
  return Math.max(min, parsed);
}

function TargetWeights({ result }: { result: RebalanceOut }) {
  return (
    <section
      aria-labelledby="target-weights"
      className="rounded-md border border-border"
      data-testid="target-weights"
    >
      <header className="flex items-center gap-2 border-b border-border px-3 py-2">
        <h2 id="target-weights" className="text-sm font-semibold tracking-tight">
          Target weights
        </h2>
        <Badge variant="neutral">{result.target_weights.length}</Badge>
        <span className="ml-auto text-xs text-muted-foreground">
          Equal weight across the portfolio after acting on the lists above.
        </span>
      </header>
      <ul className="divide-y divide-border">
        {result.target_weights.map((row) => (
          <li key={row.instrument_id} className="flex items-baseline gap-2 px-3 py-2 text-sm">
            <span className="w-8 tabular-nums text-muted-foreground">{row.rank}</span>
            <span className="min-w-0 flex-1 truncate">
              <span className="font-medium">{row.symbol}</span>{" "}
              <span className="text-muted-foreground">{row.name}</span>
            </span>
            <Badge variant={row.action === "enter" ? "positive" : "neutral"}>{row.action}</Badge>
            <span className="w-20 text-right tabular-nums">
              {(Number(row.weight) * 100).toFixed(2)}%
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

interface HistoryProps {
  portfolioId: number;
  rows: readonly {
    id: number;
    as_of: string;
    top_n: number;
    hold_buffer: number;
    screen_name?: string | null;
    entries: number;
    exits: number;
    inside_wrh: number;
    created_at: string;
  }[];
  loading: boolean;
}

/** Prompt 14 §4: "so a user can see what they were told and when". */
function RebalanceHistory({ portfolioId, rows, loading }: HistoryProps) {
  if (loading) return <Skeleton className="h-24 rounded-md" />;
  if (rows.length === 0) return null;
  return (
    <section aria-labelledby="rebalance-history" data-testid="rebalance-history">
      <h2
        id="rebalance-history"
        className="flex items-center gap-2 text-sm font-semibold tracking-tight"
      >
        <History aria-hidden="true" className="size-4" />
        Past rebalances
      </h2>
      <ul className="mt-2 divide-y divide-border rounded-md border border-border">
        {rows.map((row) => (
          <li key={row.id} className="flex flex-wrap items-baseline gap-2 px-3 py-2 text-sm">
            <span className="tabular-nums">{row.as_of}</span>
            <span className="text-muted-foreground">
              {row.screen_name ?? "a deleted screen"} · top {row.top_n} + {row.hold_buffer}
            </span>
            <span className="ml-auto text-xs text-muted-foreground">
              {row.exits} out · {row.inside_wrh} held · {row.entries} in
            </span>
            <span className="sr-only">
              Portfolio {portfolioId}, computed {row.created_at}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

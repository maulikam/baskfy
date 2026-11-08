"use client";

import type { Route } from "next";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { formatRupees } from "@/lib/discover/metrics";
import { ApiError } from "@/lib/api/errors";
import { usePortfolios } from "@/lib/portfolios/queries";
import {
  DEFAULT_INVEST_AMOUNT,
  confirmScreenInvest,
  linkedPortfoliosForScreen,
  nextInvestName,
  previewScreenInvest,
  rebalanceFromScreen,
  type ScreenInvestPreview,
  type ScreenOrderResult,
} from "@/lib/screens/invest";
import { cn } from "@/lib/utils";

type Mode = "invest" | "rebalance" | "invest-again";

export function ScreenInvestActions({
  screenName,
  screenPublicId,
}: {
  screenName: string;
  screenPublicId: string;
  /** Kept so the existing editor call site still type-checks. Ranking is sized in Rust. */
  result?: unknown;
}) {
  const portfolios = usePortfolios();
  const linked = useMemo(
    () => linkedPortfoliosForScreen(portfolios.data, screenPublicId),
    [portfolios.data, screenPublicId],
  );
  const primary = linked[0] ?? null;
  const [mode, setMode] = useState<Mode | null>(null);

  return (
    <div className="flex flex-wrap items-center gap-2">
      {primary ? (
        <>
          <Button
            type="button"
            variant="primary"
            size="sm"
            className="rounded-full px-5"
            data-testid="screen-rebalance"
            title={`Update ${primary.name} from this screen`}
            onClick={() => setMode("rebalance")}
          >
            Rebalance
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="vaaya-pill rounded-full"
            data-testid="screen-invest-again"
            title="Create another portfolio from this screen"
            onClick={() => setMode("invest-again")}
          >
            Invest again
          </Button>
          <Link
            href={`/portfolio/${primary.id}` as Route}
            className="text-xs font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
            data-testid="screen-linked-portfolio"
          >
            {primary.name}
          </Link>
        </>
      ) : (
        <Button
          type="button"
          variant="primary"
          size="sm"
          className="rounded-full px-5"
          data-testid="screen-invest"
          title="Create a portfolio from this screen"
          onClick={() => setMode("invest")}
        >
          Invest
        </Button>
      )}

      {mode ? (
        <ScreenInvestDialog
          mode={mode}
          screenName={screenName}
          screenPublicId={screenPublicId}
          linkedName={primary?.name ?? null}
          linkedId={primary?.id ?? null}
          existingNames={linked.map((row) => row.name)}
          onClose={() => setMode(null)}
        />
      ) : null}
    </div>
  );
}

function ScreenInvestDialog({
  mode,
  screenName,
  screenPublicId,
  linkedName,
  linkedId,
  existingNames,
  onClose,
}: {
  mode: Mode;
  screenName: string;
  screenPublicId: string;
  linkedName: string | null;
  linkedId: number | null;
  existingNames: readonly string[];
  onClose: () => void;
}) {
  const isRebalance = mode === "rebalance";
  const [name, setName] = useState(() =>
    isRebalance ? (linkedName ?? screenName) : nextInvestName(screenName, existingNames),
  );
  const [amount, setAmount] = useState<number | null>(DEFAULT_INVEST_AMOUNT);
  const [mtf, setMtf] = useState(false);
  const [preview, setPreview] = useState<ScreenInvestPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [doneId, setDoneId] = useState<number | null>(null);
  const [orders, setOrders] = useState<ScreenOrderResult[]>([]);

  useEffect(() => {
    if (amount === null || amount <= 0) {
      setPreview(null);
      setPreviewError(null);
      return;
    }
    const controller = new AbortController();
    setLoadingPreview(true);
    setPreviewError(null);
    void previewScreenInvest(screenPublicId, {
      amount,
      mtf,
    })
      .then((next) => {
        if (!controller.signal.aborted) setPreview(next);
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted) return;
        setPreview(null);
        setPreviewError(
          caught instanceof ApiError
            ? caught.message
            : "The invest preview could not be calculated.",
        );
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingPreview(false);
      });
    return () => controller.abort();
  }, [amount, mtf, screenPublicId]);

  const sized = preview?.holdings.filter((row) => row.quantity != null) ?? [];
  const short =
    amount !== null &&
    amount > 0 &&
    preview !== null &&
    Number(preview.min_investment) > amount;
  const emptyReason = preview?.empty_reason ?? previewError;
  const canSubmit =
    !busy &&
    !loadingPreview &&
    amount !== null &&
    amount > 0 &&
    !short &&
    sized.length > 0 &&
    name.trim().length > 0 &&
    preview !== null &&
    preview.empty_reason === null &&
    (!isRebalance || linkedId !== null);

  async function confirm(): Promise<void> {
    if (!canSubmit || amount === null || preview === null) return;
    setError(null);
    setBusy(true);
    try {
      const input = { amount, mtf };
      const saved =
        isRebalance && linkedId !== null
          ? await rebalanceFromScreen(linkedId, { ...input, plan_id: preview.plan_id })
          : await confirmScreenInvest(screenPublicId, {
              ...input,
              name: name.trim(),
              plan_id: preview.plan_id,
            });
      if (saved.already_sent) {
        setOrders(saved.orders ?? []);
        setError(saved.empty_reason ?? "These orders were already sent.");
        return;
      }
      const id = saved.report?.portfolio.id;
      if (id === undefined) {
        setOrders(saved.orders ?? []);
        if (saved.plan_id && saved.plan_id !== preview.plan_id) {
          setPreview(saved);
        }
        setError(
          saved.empty_reason ??
            (isRebalance
              ? "The portfolio could not be updated."
              : "The portfolio was not created."),
        );
        return;
      }
      setOrders(saved.orders ?? []);
      setDoneId(id);
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : isRebalance
            ? "The portfolio could not be updated."
            : "The portfolio could not be created.",
      );
    } finally {
      setBusy(false);
    }
  }

  const title = isRebalance
    ? `Rebalance ${linkedName ?? name}`
    : mode === "invest-again"
      ? `Invest again in ${screenName}`
      : `Invest in ${screenName}`;

  return (
    <Dialog open onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent className="max-h-[min(44rem,90vh)] max-w-5xl overflow-y-auto" data-testid="screen-invest-dialog">
        <DialogTitle className="text-base font-semibold">{title}</DialogTitle>
        <DialogDescription className="mt-1 text-sm text-muted-foreground">
          {isRebalance
            ? "Replace this portfolio with the screen's current names and weights. Nothing here buys or sells anything."
            : mtf
              ? "Each selected name is bought for about the same position value. MTF changes only how much of that value you pay, at that stock's Zerodha leverage. A name without MTF is bought in cash for the same position value. Buys are market orders with market protection."
              : "Creates a portfolio under Portfolios, named after this screen. Paper tracking only — no broker order."}
        </DialogDescription>

        {doneId !== null ? (
          <div className="mt-4 space-y-3" data-testid="screen-invest-done">
            <p className="text-sm">
              {isRebalance ? "Updated" : "Created"}{" "}
              <span className="font-medium">{name.trim()}</span>.
            </p>
            {orders.length > 0 ? (
              <ul className="space-y-1 text-xs text-muted-foreground" data-testid="screen-invest-orders">
                {orders.map((row) => (
                  <li key={`${row.symbol}-${row.status}`}>
                    {row.symbol || "Order"}: {row.status}
                    {row.stop ? ` · stop ${row.stop}` : ""}
                    {row.error ? ` · ${row.error}` : ""}
                  </li>
                ))}
              </ul>
            ) : null}
            <div className="flex flex-wrap gap-2">
              <Button variant="primary" size="sm" asChild>
                <Link href={`/portfolio/${doneId}` as Route}>Open portfolio</Link>
              </Button>
              <Button variant="outline" size="sm" asChild>
                <Link href={"/portfolio/portfolios" as Route}>All portfolios</Link>
              </Button>
            </div>
          </div>
        ) : (
          <div className="mt-4 space-y-4">
            {mode === "invest-again" && linkedName ? (
              <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
                {linkedName} already comes from this screen. This creates a separate portfolio.
              </p>
            ) : null}

            {isRebalance ? null : (
              <label className="flex flex-col gap-1.5 text-sm">
                <span className="font-medium">Portfolio name</span>
                <Input
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  aria-label="Portfolio name"
                  data-testid="screen-invest-name"
                />
              </label>
            )}

            <label className="flex flex-col gap-1.5 text-sm">
              <span className="font-medium">Amount to invest</span>
              <div className="flex items-center gap-2 rounded-md border border-input bg-card px-3 focus-within:ring-2 focus-within:ring-ring">
                <span className="text-muted-foreground">₹</span>
                <input
                  type="number"
                  inputMode="numeric"
                  min={0}
                  step={1000}
                  value={amount ?? ""}
                  onChange={(event) => {
                    const raw = event.target.value;
                    setAmount(raw === "" ? null : Math.max(0, Number(raw) || 0));
                  }}
                  aria-label="Amount to invest"
                  aria-describedby="screen-invest-amount-help"
                  data-testid="screen-invest-amount"
                  className="w-full bg-transparent py-2 text-sm tabular-nums outline-none"
                />
              </div>
              <span
                id="screen-invest-amount-help"
                className={cn(
                  "text-xs",
                  short ? "text-warning-foreground" : "text-muted-foreground",
                )}
              >
                {preview
                  ? short
                    ? `${formatRupees(Number(preview.min_investment))} is the least that fills these names.`
                    : `${formatRupees(Number(preview.min_investment))} minimum cash for ${preview.holdings.length} names.`
                  : loadingPreview
                    ? "Sizing from the latest screen run…"
                    : "Enter an amount to size the portfolio."}
              </span>
            </label>

            <label className="flex items-start gap-3 text-sm">
              <input
                type="checkbox"
                checked={mtf}
                onChange={(event) => setMtf(event.target.checked)}
                data-testid="screen-invest-mtf"
                className="mt-1"
              />
              <span>
                <span className="font-medium">Use MTF</span>
                <span className="mt-0.5 block text-xs text-muted-foreground">
                  {isRebalance
                    ? "Each name is sized for a similar amount. Rebalance does not send another broker order."
                    : "Each name is bought for about the same position value. MTF changes only the cash that value needs. A name without MTF is bought in cash for that same value. Your cash is not split equally and then levered."}
                </span>
              </span>
            </label>

            {preview && mtf ? (
              <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs" data-testid="screen-invest-mtf-summary">
                Position target {formatRupees(Number(preview.gross_target))} before share rounding.
                Cash contributions differ when leverage differs.
              </p>
            ) : null}

            <div>
              <p className="text-sm font-medium">Review holdings</p>
              <p className="text-xs text-muted-foreground">
                {preview
                  ? `${sized.length} of ${preview.holdings.length} names get at least one share.${
                      Number(preview.leftover) > 0
                        ? ` ${formatRupees(Number(preview.leftover))} stays unallocated.`
                        : ""
                    }`
                  : loadingPreview
                    ? "Loading the latest ranks…"
                    : "The review fills after the amount is sized."}
              </p>
              {emptyReason ? (
                <p
                  className="mt-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-sm text-muted-foreground"
                  data-testid="screen-invest-empty-holdings"
                >
                  {emptyReason}
                </p>
              ) : null}
              {preview?.constraint ? (
                <p className="mt-2 text-xs text-muted-foreground" data-testid="screen-invest-constraint">
                  {preview.constraint}
                </p>
              ) : null}
              {preview && preview.holdings.length > 0 ? (
                <div className="mt-2 max-h-72 overflow-auto rounded-md border border-border">
                  <table className="w-full min-w-[52rem] text-left text-sm">
                    <thead className="sticky top-0 bg-card text-xs text-muted-foreground">
                      <tr>
                        <th className="px-3 py-2 font-medium">Symbol</th>
                        <th className="px-3 py-2 font-medium">Product</th>
                        <th className="px-3 py-2 text-right font-medium">Qty</th>
                        <th className="px-3 py-2 text-right font-medium">Est. price</th>
                        <th className="px-3 py-2 text-right font-medium">Position</th>
                        <th className="px-3 py-2 text-right font-medium">My cash</th>
                        <th className="px-3 py-2 text-right font-medium">Broker</th>
                        <th className="px-3 py-2 text-right font-medium">Lev</th>
                        <th className="px-3 py-2 text-right font-medium">Weight</th>
                        <th className="px-3 py-2 text-right font-medium">Vs target</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.holdings.map((row) => (
                        <tr key={row.symbol} className="border-t border-border/70">
                          <td className="px-3 py-1.5 font-medium">{row.symbol}</td>
                          <td className="px-3 py-1.5 text-muted-foreground">{row.product}</td>
                          <td className="px-3 py-1.5 text-right tabular-nums">{row.quantity ?? "—"}</td>
                          <td className="px-3 py-1.5 text-right tabular-nums">
                            {formatRupees(row.price)}
                          </td>
                          <td className="px-3 py-1.5 text-right tabular-nums">
                            {formatRupees(Number(row.amount))}
                          </td>
                          <td className="px-3 py-1.5 text-right tabular-nums">
                            {formatRupees(Number(row.contribution))}
                          </td>
                          <td className="px-3 py-1.5 text-right tabular-nums">
                            {formatRupees(Number(row.funding))}
                          </td>
                          <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
                            {row.product === "MTF" ? `${row.leverage}×` : "Cash"}
                          </td>
                          <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
                            {(Number(row.weight) * 100).toFixed(1)}%
                          </td>
                          <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
                            {formatRupees(Number(row.deviation))}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}
              {preview && sized.length > 0 ? (
                <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3" data-testid="screen-invest-totals">
                  <div className="flex justify-between gap-2"><dt>Cash budget</dt><dd className="tabular-nums">{formatRupees(Number(preview.cash_outlay))}</dd></div>
                  <div className="flex justify-between gap-2"><dt>My contribution</dt><dd className="tabular-nums">{formatRupees(Number(preview.contribution))}</dd></div>
                  <div className="flex justify-between gap-2"><dt>Estimated costs</dt><dd className="tabular-nums">{formatRupees(Number(preview.costs))}</dd></div>
                  <div className="flex justify-between gap-2"><dt>Reserved cash</dt><dd className="tabular-nums">{formatRupees(Number(preview.reserve))}</dd></div>
                  <div className="flex justify-between gap-2"><dt>Unallocated cash</dt><dd className="tabular-nums">{formatRupees(Number(preview.leftover))}</dd></div>
                  <div className="flex justify-between gap-2"><dt>Gross exposure</dt><dd className="tabular-nums">{formatRupees(Number(preview.exposure))}</dd></div>
                  <div className="flex justify-between gap-2"><dt>Broker funding</dt><dd className="tabular-nums">{formatRupees(Number(preview.funding))}</dd></div>
                </dl>
              ) : null}
              {preview && preview.skipped.length > 0 ? (
                <ul className="mt-2 space-y-1 text-xs text-muted-foreground" data-testid="screen-invest-skipped">
                  {preview.skipped.map((row) => (
                    <li key={`${row.symbol}-${row.reason}`}>
                      {row.symbol}:{" "}
                      {row.reason === "mtf_ineligible"
                        ? "not MTF-eligible"
                        : row.reason === "leverage_unverified"
                          ? "leverage was not verified"
                          : row.reason === "no_price"
                            ? "no price"
                            : row.reason}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>

            {error ? (
              <p role="alert" className="text-sm text-warning-foreground" data-testid="screen-invest-error">
                {error}
              </p>
            ) : null}
            {orders.length > 0 ? (
              <ul className="space-y-1 text-xs text-muted-foreground" data-testid="screen-invest-orders">
                {orders.map((row) => (
                  <li key={`${row.symbol}-${row.status}`}>
                    {row.symbol || "Order"}: {row.status}
                    {row.stop ? ` · stop ${row.stop}` : ""}
                    {row.error ? ` · ${row.error}` : ""}
                  </li>
                ))}
              </ul>
            ) : null}

            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="primary"
                size="sm"
                disabled={!canSubmit}
                data-testid="screen-invest-confirm"
                onClick={() => void confirm()}
              >
                {busy
                  ? isRebalance
                    ? "Updating…"
                    : "Creating…"
                  : isRebalance
                    ? "Confirm rebalance"
                    : mtf
                      ? "Send MTF orders"
                      : "Confirm invest"}
              </Button>
              <Button type="button" variant="ghost" size="sm" onClick={onClose}>
                Cancel
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

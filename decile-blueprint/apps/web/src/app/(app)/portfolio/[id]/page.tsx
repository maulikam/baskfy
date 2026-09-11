import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { DriftRepair } from "@/components/investments/drift-repair";
import { InvestmentActions } from "@/components/investments/investment-actions";
import { ShowDetailsModal } from "@/components/investments/show-details-modal";
import { SipForm } from "@/components/investments/sip-form";
import { RebalanceSlot } from "@/components/portfolio/detail/detail-slots";
import { PortfolioDetailWorkspace } from "@/components/portfolio/detail/workspace";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { EMPTY_CELL, formatNumber, formatPercent, formatTradeDate } from "@/lib/format";
import { fetchInvestment, type InvestmentDetail } from "@/lib/investments/fetch";
import { loadPortfolioDetail, numericPortfolioId } from "@/lib/portfolio/detail-fetch";

/**
 * `/portfolio/[id]` — `PORTFOLIO_REDESIGN.md` §7's detail page.
 *
 * ## Why this route serves two payloads
 *
 * §2 merges "Investments" and "Portfolios" into one object, so post-merge an investment **is** a
 * portfolio and this is its page. The merge is complete in the object model and not yet complete
 * in the data: `GET /portfolio/{id}` answers for a ledger portfolio by its integer id, while the
 * older `/cb/investments/{id}` still answers for rows the ledger has not absorbed, under ids that
 * are not integers at all.
 *
 * So the route tries the §7 read whenever the parameter could be a portfolio id, and falls back
 * to the investment ledger when it could not or when the ledger did not answer. Sending a reader
 * to a 404 for a portfolio they can see listed on the overview would be a worse outcome than a
 * reduced page, and deleting the older view before the newer one covers every row would delete
 * working behaviour to make a point about architecture.
 *
 * The manage actions — SIP, drift repair, the customize/costs/orders links — are still owned by
 * the investment ledger and are composed **into** the §7 screen when both payloads exist, rather
 * than being dropped. None of them places an order: §9's rule is that a rebalance produces a plan
 * the reader takes to their broker, and every CTA below ends in a plan handoff.
 */

export const dynamic = "force-dynamic";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<Metadata> {
  const { id } = await params;
  const portfolioId = numericPortfolioId(id);
  if (portfolioId !== null) {
    const bundle = await loadPortfolioDetail(portfolioId);
    if (bundle.detail !== null) {
      return {
        title: bundle.detail.summary.name,
        robots: { index: false, follow: false },
      };
    }
  }
  const investment = await fetchInvestment(id);
  return {
    title: investment?.basket_name ?? "Portfolio",
    robots: { index: false, follow: false },
  };
}

export default async function PortfolioDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const portfolioId = numericPortfolioId(id);

  const [bundle, investment] = await Promise.all([
    portfolioId === null ? Promise.resolve(null) : loadPortfolioDetail(portfolioId),
    fetchInvestment(id),
  ]);

  if (bundle === null || bundle.detail === null) {
    if (investment === null) notFound();
    return <ReducedView detail={investment} unavailableReason={bundle?.failures.detail ?? null} />;
  }

  return (
    <div className="flex flex-col gap-6">
      <SectionTabs section="portfolio" />
      {/* PC3's eight tabs replace the single scrolling detail screen. The workspace renders its
          own heading, source badge and hide-amounts toggle, so the page adds none of them.
          `rebalanceSlot` is PC4's drawer, joined in `detail-slots` because §6.1 forbids either
          leaf from importing the other. */}
      <PortfolioDetailWorkspace
        detail={bundle.detail}
        nav={bundle.nav}
        activity={bundle.activity}
        failures={{ nav: bundle.failures.nav, activity: bundle.failures.activity }}
        rebalanceSlot={
          <RebalanceSlot
            portfolioId={bundle.detail.summary.portfolio_id}
            portfolioName={bundle.detail.summary.name}
            detail={bundle.detail}
          />
        }
      >
        {investment ? <ManageSection detail={investment} /> : null}
        <p className="text-xs text-muted-foreground">
          Read-only. Every action here ends in an order plan you take to your broker; this page
          never places an order.{" "}
          <Link href="/disclaimer" className="underline-offset-4 hover:underline">
            Disclaimer
          </Link>
        </p>
      </PortfolioDetailWorkspace>
    </div>
  );
}

/**
 * The manage actions the investment ledger still owns.
 *
 * Kept verbatim from the page this one replaces. §7 does not describe them because §7 describes
 * what a portfolio *shows*, not what the older ledger can still do to one; removing them would
 * have left the SIP schedule and the drift repair with no route at all.
 */
function ManageSection({ detail }: { detail: InvestmentDetail }) {
  return (
    <section className="flex flex-col gap-3 rounded-xl border border-border/70 bg-card p-4">
      <h2 className="text-sm font-semibold">Manage</h2>
      <ul className="space-y-2 text-sm text-muted-foreground">
        {detail.basket_slug ? (
          <li>
            <Link
              href={`/basket/${detail.basket_slug}/constituents`}
              className="text-foreground underline-offset-4 hover:underline"
            >
              Constituents
            </Link>{" "}
            — read-only model weights
          </li>
        ) : null}
        <li>
          <Link
            href={`/portfolio/${detail.id}/customize`}
            className="text-foreground underline-offset-4 hover:underline"
          >
            Customize
          </Link>{" "}
          — a weight-difference plan preview; no broker path from here
        </li>
        <li>
          <Link
            href={`/portfolio/${detail.id}/costs`}
            className="text-foreground underline-offset-4 hover:underline"
          >
            Costs and returns
          </Link>{" "}
          — accrued fees, not charged
        </li>
        <li>
          <Link
            href={`/portfolio/${detail.id}/orders`}
            className="text-foreground underline-offset-4 hover:underline"
          >
            Orders
          </Link>{" "}
          — read-only batch list
        </li>
      </ul>
      <SipForm investmentId={detail.id} />
      <DriftRepair investmentId={detail.id} />
      <InvestmentActions basketName={detail.basket_name} />
    </section>
  );
}

/**
 * What the reader gets when §7's read did not answer for this id: the older ledger view.
 *
 * No unlabelled percentages reach this page either. The older payload's `current_returns_pct` has
 * no metric kind and no start date on the wire, so it is labelled here with what it actually is —
 * a change against the money put in, since the ledger's own start — rather than printed bare,
 * which is what §11 criterion 3 forbids.
 */
function ReducedView({
  detail,
  unavailableReason,
}: {
  detail: InvestmentDetail;
  unavailableReason: string | null;
}) {
  const snap = detail.snapshot;
  /* Criterion 3 applies to the reduced view too: this payload has no metric kind and no start
     date on the wire, so the one date it does carry is attached to every rate it shows. */
  const since = detail.invested_at
    ? `Measured since ${formatTradeDate(detail.invested_at)}.`
    : "Start date not recorded.";

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <SectionTabs section="portfolio" />
      <PageHeader
        title={detail.basket_name}
        blurb="Holdings, returns, and manage actions — plans hand off to the desk; nothing executes here."
        meta={
          <span className="text-xs text-muted-foreground">
            Status {detail.status}
            {detail.basket_slug ? (
              <>
                {" · "}
                <Link
                  href={`/basket/${detail.basket_slug}`}
                  className="underline-offset-4 hover:underline"
                >
                  Model page
                </Link>
              </>
            ) : null}
          </span>
        }
        actions={<ShowDetailsModal basketName={detail.basket_name} snapshot={snap} />}
      />

      <p
        role="status"
        data-testid="detail-reduced-view"
        className="rounded-xl border border-warning/40 bg-warning-muted px-4 py-2.5 text-sm"
      >
        {unavailableReason ??
          "This portfolio has not been rebuilt into the allocation ledger yet, so this is the older view of it."}{" "}
        The chart, the per-holding contributions and the source panel are missing rather than
        wrong.
      </p>

      {detail.rebalance_pending ? (
        <div className="rounded-xl border border-accent/40 bg-accent-muted/40 px-4 py-3 text-sm">
          <p className="font-medium text-foreground">Model update available</p>
          <p className="mt-1 text-muted-foreground">
            Applying it builds an order plan from the weight difference — it does not place orders
            from this page.
          </p>
        </div>
      ) : null}

      <section aria-label="Performance" className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Current value" value={rupees(snap?.current_value)} />
        <Stat label="Money put in" value={rupees(snap?.money_put_in)} />
        <Stat
          label="Change against money put in, since this ledger started"
          value={snap == null ? EMPTY_CELL : formatPercent(snap.current_returns_pct)}
          hint={since}
        />
        <Stat
          label="XIRR since your first purchase"
          value={
            snap?.xirr_displayable && snap.xirr != null ? formatPercent(snap.xirr, 2) : EMPTY_CELL
          }
          hint={since}
        />
      </section>

      <section className="space-y-2">
        <h2 className="text-sm font-semibold">What this portfolio holds</h2>
        {detail.holdings.length === 0 ? (
          <p className="text-sm text-muted-foreground">No holdings are recorded here yet.</p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-border text-xs text-muted-foreground">
                <th className="py-2 font-medium">Symbol</th>
                <th className="py-2 font-medium">Qty</th>
                <th className="py-2 font-medium">Weight</th>
                <th className="py-2 font-medium" title={since}>
                  Change since this ledger started
                </th>
              </tr>
            </thead>
            <tbody>
              {detail.holdings.map((row) => (
                <tr key={row.symbol} className="border-b border-border/60">
                  <td className="py-2 font-medium">{row.symbol}</td>
                  <td className="py-2 tabular-nums">{row.qty}</td>
                  <td className="py-2 tabular-nums">
                    {row.weight == null ? EMPTY_CELL : formatPercent(row.weight)}
                  </td>
                  <td className="py-2 tabular-nums">
                    {row.returns_pct == null ? EMPTY_CELL : formatPercent(row.returns_pct)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <ManageSection detail={detail} />

      <p className="text-xs text-muted-foreground">
        Read-only. This page never places an order.{" "}
        <Link href="/disclaimer" className="underline-offset-4 hover:underline">
          Disclaimer
        </Link>
      </p>
    </div>
  );
}

function rupees(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  return `₹${formatNumber(value, { decimals: 2 })}`;
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div title={hint ? `${label}. ${hint}` : undefined}>
      <div className="eyebrow">{label}</div>
      <div className="mt-0.5 text-lg font-semibold tabular-nums">{value}</div>
      {hint ? <div className="mt-0.5 text-[11px] text-muted-foreground">{hint}</div> : null}
    </div>
  );
}

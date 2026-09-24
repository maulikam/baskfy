import type { Metadata } from "next";

import { NotASignalBanner, Tier2ECaveat } from "@/components/fno/caveats";
import { InfoTable } from "@/components/fno/info-table";
import { FnoRiskCaveat } from "@/components/options/caveats";
import { ToneBadge } from "@/components/options/state-chip";
import { PageHeader } from "@/components/shell/page-header";
import { OptionsSubNav } from "@/components/shell/section-tabs";
import { formatTradeDate } from "@/lib/format";
import { fetchFnoInfo } from "@/lib/fno/fetch";
import type { FnoInfo } from "@/lib/fno/types";
import { asOfClose, signed } from "@/lib/fno/view";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/options/fno` — the Stock F&O information page (`docs/fno/05` §3, `04` §5, FO5). Facts about
 * every F&O underlying after the close, and never a candidate (PACK.8): the banner says what the
 * research found about these numbers before the table shows them.
 *
 * **The clock** (root `CLAUDE.md`'s two-clock table, Stock F&O row): every column is the last
 * completed session's bhavcopy and the series derived from it — `As of close, <date>`. `05` §3
 * allows the price column the shared live overlay; this table's price is the *futures settle*, and
 * a cash last price is not a settle, so no column is overlaid (DECISIONS-FO FO5.5).
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/options/fno"].title,
  description: PAGES["/options/fno"].blurb,
};

function Families({ info }: { info: FnoInfo }) {
  return (
    <section
      id="families"
      className="space-y-3"
      aria-labelledby="fno-families-heading"
    >
      <h2 id="fno-families-heading" className="text-sm font-semibold">
        Families tested and rejected
      </h2>
      <p className="text-xs text-muted-foreground">
        The research&rsquo;s verdict table (
        <span className="font-mono">RESEARCH.md</span>, run{" "}
        {formatTradeDate(info.research_run_date)} over {info.research_sample}),
        with the latest quarterly re-test beside each row.
      </p>
      <ul className="space-y-3" data-testid="fno-families">
        {info.families.map(({ verdict, latest_retest: retest }) => (
          <li
            key={verdict.family}
            className="space-y-1 rounded-lg border border-border/70 p-3 text-sm"
          >
            <p className="font-medium">{verdict.family}</p>
            <p className="text-xs text-muted-foreground">
              {verdict.best_variant} · net {verdict.net_r} R a trade · years:{" "}
              {verdict.robust}
            </p>
            <p>{verdict.verdict}</p>
            {retest ? (
              <div
                className="space-y-1 text-xs"
                data-testid="fno-family-retest"
              >
                <p>
                  Re-test {formatTradeDate(retest.run_at.slice(0, 10))}:{" "}
                  {signed(retest.net_r, 3)}R, n = {retest.n},{" "}
                  {formatTradeDate(retest.sample_from)} to{" "}
                  {formatTradeDate(retest.sample_to)}, slippage{" "}
                  {retest.slippage_source.toLowerCase()}.
                </p>
                <Tier2ECaveat text={retest.caveat} />
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">
                No re-test has run yet.
              </p>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function DataStatus({ info }: { info: FnoInfo }) {
  const { spread_sample: spreads, ingest } = info;
  return (
    <section
      className="space-y-1 text-xs text-muted-foreground"
      data-testid="fno-data-status"
    >
      <p>
        Measured spreads (the 15:00 sample):{" "}
        {spreads.sessions === 0
          ? "none yet."
          : `${spreads.sessions} sessions over ${spreads.symbols} underlyings, ${formatTradeDate(spreads.first)} to ${formatTradeDate(spreads.last)}.`}
      </p>
      <p>
        Bhavcopy ingest: last{" "}
        {ingest.latest_date ? formatTradeDate(ingest.latest_date) : "never"}
        {ingest.latest_status ? ` (${ingest.latest_status.toLowerCase()})` : ""}
        .{" "}
        {ingest.missing_days.length === 0
          ? "No missing session in the last 90 days."
          : `Missing, never interpolated: ${ingest.missing_days.map((d) => formatTradeDate(d)).join(", ")}.`}
      </p>
      {ingest.ban_for_session ? (
        <p>
          Ban list for {formatTradeDate(ingest.ban_for_session)}:{" "}
          {ingest.ban_symbols.length === 0
            ? "empty"
            : ingest.ban_symbols.join(", ")}
          .
        </p>
      ) : null}
    </section>
  );
}

export default async function StockFnoPage() {
  const info = await fetchFnoInfo();

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/options/fno"].title}
        blurb={PAGES["/options/fno"].blurb}
        meta={
          info ? (
            <ToneBadge
              text={asOfClose(info.as_of)}
              tone="neutral"
              testId="fno-clock"
            />
          ) : null
        }
      />
      <OptionsSubNav />

      <NotASignalBanner />
      <FnoRiskCaveat />

      {info === null ? (
        <p className="text-sm">
          The F&amp;O service did not answer, so this page has nothing to show
          right now.
        </p>
      ) : (
        <>
          {info.rows.length === 0 ? (
            <p
              className="text-sm text-muted-foreground"
              data-testid="fno-info-empty"
            >
              No F&amp;O series has been derived yet. It is written each evening
              after the F&amp;O bhavcopy is ingested.
            </p>
          ) : (
            <InfoTable rows={info.rows} />
          )}
          <DataStatus info={info} />
          <Families info={info} />
        </>
      )}
    </div>
  );
}

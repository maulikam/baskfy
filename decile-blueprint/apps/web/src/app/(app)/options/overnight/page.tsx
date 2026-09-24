import type { Metadata } from "next";

import { DeskOnlyPrices } from "@/components/fno/caveats";
import {
  EvidenceCard,
  F1Card,
  F2Section,
  JournalTables,
  OpenStructures,
} from "@/components/fno/overnight";
import { FnoRiskCaveat, ScanOnly } from "@/components/options/caveats";
import { ToneBadge } from "@/components/options/state-chip";
import { LiveMarksProvider } from "@/components/screens/live-price";
import { Answer } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { OptionsSubNav } from "@/components/shell/section-tabs";
import { fetchFnoOvernight } from "@/lib/fno/fetch";
import { asOfClose, emptyReasonText } from "@/lib/fno/view";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/options/overnight` — F1 and F2 (`docs/fno/05` §2, FO5). Read-only: the desk's F&O page is the
 * only surface with a Confirm (`02` Track C §4), and this page binds no action.
 *
 * **The clock is on the page** (root `CLAUDE.md`'s two-clock table, Overnight row): the scan
 * state, the proposed condor and IV/RV are the last completed session's bhavcopy
 * (`As of close, Tue 22 Sep`); open structures are marked at that session's settle
 * (`Marked at settle, 22 Sep`); the one live number is the underlying's level, from the shared
 * `useLiveMarks` overlay during market hours (`NIFTY live 13:14`). No option is priced live here.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/options/overnight"].title,
  description: PAGES["/options/overnight"].blurb,
};

const LIVE_SYMBOLS = ["NIFTY 50", "NIFTY BANK"] as const;

export default async function OvernightPage() {
  const view = await fetchFnoOvernight();
  const empty = view ? emptyReasonText(view.empty_reason) : null;

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/options/overnight"].title}
        blurb={PAGES["/options/overnight"].blurb}
        meta={
          <span className="flex flex-wrap items-center gap-2">
            {view ? (
              <ToneBadge
                text={asOfClose(view.scan_date)}
                tone="neutral"
                testId="fno-clock"
              />
            ) : null}
            {view?.gates.map((gate) => (
              <ToneBadge
                key={gate.group}
                text={`${gate.group} · ${gate.mode}`}
                tone={gate.mode === "LIVE" ? "warning" : "neutral"}
              />
            ))}
            <ScanOnly />
          </span>
        }
      />
      <OptionsSubNav />

      <FnoRiskCaveat />

      <Answer
        footnote={
          "The scan reads the last completed session's F&O bhavcopy after the close; open " +
          "structures are marked at that session's settle. Only the index level may update live. " +
          "A plan is confirmed on the desk, by hand, and nothing on this page can place an order."
        }
      >
        {view === null ? (
          <>
            The F&amp;O service did not answer, so this page has nothing to show
            right now.
          </>
        ) : empty ? (
          <span data-testid="fno-empty-reason">{empty}</span>
        ) : (
          <>
            F1&rsquo;s two underlyings and F2&rsquo;s candidates, from the last
            close.
          </>
        )}
      </Answer>

      {view ? (
        <>
          {view.hard_exit_tomorrow.length > 0 ? (
            <p
              role="alert"
              data-testid="fno-hard-exit-tomorrow"
              className="rounded-md border border-negative/50 bg-negative-muted px-3 py-2 text-sm font-medium text-negative"
            >
              Hard exit tomorrow 15:00 —{" "}
              {view.hard_exit_tomorrow.map((p) => p.symbol).join(", ")}
            </p>
          ) : null}

          <section className="space-y-3" aria-labelledby="fno-f1-heading">
            <h2
              id="fno-f1-heading"
              className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
            >
              F1 · Index monthly condor (paper)
            </h2>
            <LiveMarksProvider symbols={LIVE_SYMBOLS}>
              <div className="grid gap-3 lg:grid-cols-2">
                {view.underlyings.map((underlying) => (
                  <F1Card
                    key={underlying.symbol}
                    underlying={underlying}
                    scanDate={view.scan_date}
                  />
                ))}
              </div>
            </LiveMarksProvider>
            <DeskOnlyPrices />
          </section>

          <OpenStructures positions={view.open_structures} />
          <JournalTables rows={view.journal} title="F1 journal" />
          <EvidenceCard evidence={view.evidence} />
          <F2Section f2={view.f2} />
        </>
      ) : null}
    </div>
  );
}

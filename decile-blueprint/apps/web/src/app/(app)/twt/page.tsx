import type { Metadata } from "next";

import { Answer, Mark } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { GateCard } from "@/components/twt/gate-card";
import { HalfSizeCounter } from "@/components/twt/half-size-counter";
import { OpenPositions } from "@/components/twt/open-positions";
import { TightNames } from "@/components/twt/tight-names";
import { formatTradeDate } from "@/lib/format";
import { breadthLine, gateMeaning } from "@/lib/twt/copy";
import { fetchToday } from "@/lib/twt/fetch";
import { todayView } from "@/lib/twt/view";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/twt` — the hub of `docs/twt/05` §1.
 *
 * Three things in the order a person asks for them: **may it buy at all** (the gate), **which
 * names are quiet enough to buy** and **what is already open, and how far it is from its stop**.
 * The gate is first because a list of candidates under a shut gate is a list of trades not to
 * take, and a reader who meets the list first is a reader who has already started choosing.
 *
 * **Nothing on this page mutates anything.** There is no `actions.ts` under this tree and there
 * is not going to be one: `docs/twt/02` Track C §4 gives the web app no route under `/twt` that
 * can reach the gateway, and `05` §2 says a plan line becomes an order in the desk console, on a
 * click a person makes, and nowhere else. `__tests__/read-only.test.tsx` asserts that over the
 * whole tree — including that every method other than GET on this route is refused, which is what
 * a route with no handler and no action already is.
 *
 * **Built ahead of its data (TW8).** TW4 and TW5 have not landed, so `fetchToday` answers `null`
 * and every section renders its empty state. That is deliberate: the empty state is the state
 * this page will actually be in for its first weeks, and it is the one most worth getting right.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/twt"].title,
  description: PAGES["/twt"].blurb,
};

export default async function TwtPage() {
  const today = await fetchToday();
  const view = todayView(today);
  const gate = today?.gate ?? null;

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/twt"].title}
        blurb={PAGES["/twt"].blurb}
        meta={
          view.gate.session ? (
            <span>As of {formatTradeDate(view.gate.session)}</span>
          ) : null
        }
      />
      <SectionTabs section="twt" />

      <Answer
        footnote={
          "Read from published end-of-day prices — a daily price is a closed day, so this is the " +
          "last completed session and not today. Open positions are marked at the live price. " +
          "Nothing here is advice, and nothing on this page can place an order."
        }
      >
        {gate && gate.gate !== null ? (
          <>
            New entries are{" "}
            <span data-testid="twt-answer-gate">
              <Mark>{gate.gate === "OPEN" ? "allowed" : "not allowed"}</Mark>
            </span>{" "}
            today &mdash; {gateMeaning(gate.gate)}. <Mark>{breadthLine(gate)}</Mark>.
          </>
        ) : (
          <>Nothing has been read for this strategy yet, so it has nothing to say about today.</>
        )}
      </Answer>

      <GateCard gate={gate} />

      <TightNames rows={view.tight} session={view.gate.session} />

      <OpenPositions rows={view.positions} />

      {today ? <HalfSizeCounter halfSize={today.half_size} /> : null}
    </div>
  );
}

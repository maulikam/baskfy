import type { Metadata } from "next";

import { FnoRiskCaveat } from "@/components/options/caveats";
import { PageHeader } from "@/components/shell/page-header";
import { OptionsSubNav, SectionTabs } from "@/components/shell/section-tabs";
import { Button } from "@/components/ui/button";
import { formatTradeDate } from "@/lib/format";
import { fetchOptionsCalendar } from "@/lib/options/fetch";
import { reasonText } from "@/lib/options/view";
import { PAGES } from "@/lib/vocabulary";

import { addEventDay, removeEventDay } from "./actions";
import { EventDayForm } from "./_components/event-day-form";

/**
 * `/options/calendar` — the year's NIFTY expiries (from the NFO contract list, `op_expiry`: no
 * weekday rule, a holiday-shifted expiry moves with it) and the event days no sleeve trades.
 * One of the tab's two writes lives here: add or remove a day **you** added. Neither half can
 * move money; an event day only ever stops a sleeve from trading.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/options/calendar"].title,
  description: PAGES["/options/calendar"].blurb,
};

export default async function OptionsCalendarPage() {
  const calendar = await fetchOptionsCalendar();
  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/options/calendar"].title}
        blurb={PAGES["/options/calendar"].blurb}
      />
      <OptionsSubNav />
      <SectionTabs section="options" />
      <FnoRiskCaveat />

      <section aria-labelledby="options-expiries-heading" className="space-y-3">
        <h2
          id="options-expiries-heading"
          className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
        >
          Expiries{calendar ? ` in ${calendar.year}` : ""}
        </h2>
        {!calendar || calendar.expiries.length === 0 ? (
          <p className="text-sm" data-testid="options-calendar-empty">
            The NIFTY contract list has not been loaded yet, so no expiry dates
            are known. They come from the exchange&rsquo;s own list each night,
            never from a weekday rule.
          </p>
        ) : (
          <ol
            className="grid gap-1 text-sm sm:grid-cols-2 lg:grid-cols-3"
            data-testid="options-calendar-expiries"
          >
            {calendar.expiries.map((expiry) => (
              <li key={expiry.expiry_date} className="tabular-nums">
                {formatTradeDate(expiry.expiry_date)}{" "}
                <span className="text-xs text-muted-foreground">
                  {expiry.kind === "MONTHLY" ? "monthly" : "weekly"} · lot{" "}
                  {expiry.lot_size}
                </span>
                {expiry.event_day ? (
                  <span className="ml-1 text-xs text-warning">· event day</span>
                ) : null}
              </li>
            ))}
          </ol>
        )}
      </section>

      <section aria-labelledby="options-events-heading" className="space-y-3">
        <h2
          id="options-events-heading"
          className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
        >
          Event days — no options strategy trades these
        </h2>
        {!calendar || calendar.event_days.length === 0 ? (
          <p className="text-sm">No event day is set.</p>
        ) : (
          <ul className="space-y-1 text-sm" data-testid="options-event-days">
            {calendar.event_days.map((day) => (
              <li
                key={day.date}
                className="flex flex-wrap items-center justify-between gap-2"
              >
                <span>
                  {formatTradeDate(day.date)} ·{" "}
                  {day.reason === "MANUAL"
                    ? "added by you"
                    : reasonText(day.reason).toLowerCase()}
                  {day.note ? (
                    <span className="text-muted-foreground"> · {day.note}</span>
                  ) : null}
                  {day.source === "SEED" ? (
                    <span className="text-xs text-muted-foreground">
                      {" "}
                      · verified against the published calendar
                    </span>
                  ) : null}
                </span>
                {day.removable ? (
                  <form action={removeEventDay}>
                    <input type="hidden" name="date" value={day.date} />
                    <Button type="submit" variant="outline" size="sm">
                      Remove
                    </Button>
                  </form>
                ) : null}
              </li>
            ))}
          </ul>
        )}
        <EventDayForm action={addEventDay} />
      </section>
    </div>
  );
}

"use client";

import type { Route } from "next";
import Link from "next/link";

import { ReturnValue } from "@/components/portfolio/return-value";
import { formatTradeDate } from "@/lib/format";
import type { PortfolioSummary, SourcePanel } from "@/lib/portfolio/detail-view";
import { fromFigure } from "@/lib/portfolio/overview";

/**
 * §7's source panel — the block that differs by §3 source, and the block §9 governs.
 *
 * §7 asks for four different panels:
 *
 * * **Subscribed** — publisher, methodology, model-vs-actual, rebalance instructions.
 * * **My screen** — the rules, last run, entries and exits.
 * * **My strategy** — the same three, from the saved strategy.
 * * **Holding group** — included brokers, and the date it was grouped on.
 *
 * ## §9 decides the words, and the server already chose them
 *
 * Baskfy is not SEBI-registered. "Managed", "managed portfolio", "advisory" and "PMS" describe a
 * service it does not provide, and using one of them about somebody else's model would be a
 * regulatory claim rather than a UI choice. `SourcePanelOut.headline` arrives already spelled
 * *"Subscribed model by {publisher}"* — composed on the server, where a test asserts the wording
 * over rendered payloads — and this component prints it. It does not assemble a second version,
 * because the second version is the one free to drift.
 *
 * ## Model-vs-actual, in the one shape criterion 5 allows
 *
 * §7 asks a subscribed panel for "model-vs-actual". That is a *comparison*, and criterion 5
 * forbids the obvious way to draw one: `model_return` and `headline_return` are never one figure
 * and never a difference between the two. So the comparison is two rows, each rendered by
 * {@link ReturnValue} with its own label and its own start date, one of them marked as the
 * publisher's record — plus the sentence saying why they differ, which is the thing a reader
 * actually wants when the two do not match.
 *
 * ## Nothing here executes
 *
 * §9: a rebalance produces an order plan the reader takes to their broker. `execution_note` is
 * the API's own sentence for that and it is printed on every source, not only the subscribed one.
 */

export interface DetailSourcePanelProps {
  panel: SourcePanel;
  summary: PortfolioSummary;
}

export function DetailSourcePanel({ panel, summary }: DetailSourcePanelProps) {
  return (
    <section
      aria-label="Where this portfolio comes from"
      data-testid="detail-source-panel"
      data-source={panel.source}
      className="flex flex-col gap-3 rounded-xl border border-border/70 bg-card p-4"
    >
      <div>
        <h2 className="text-sm font-semibold">Where this portfolio comes from</h2>
        <p data-testid="detail-source-headline" className="mt-1 text-sm text-muted-foreground">
          {panel.headline}
        </p>
      </div>

      {panel.source === "SUBSCRIBED" ? (
        <SubscribedBody panel={panel} summary={summary} />
      ) : panel.source === "HOLDING_GROUP" ? (
        <HoldingGroupBody panel={panel} />
      ) : (
        <RulesBody panel={panel} />
      )}

      <p data-testid="detail-execution-note" className="text-xs text-muted-foreground">
        {panel.execution_note}
      </p>
    </section>
  );
}

function SubscribedBody({ panel, summary }: DetailSourcePanelProps) {
  return (
    <div className="flex flex-col gap-3" data-testid="detail-source-subscribed">
      <Row label="Published by">{panel.publisher ?? "an unnamed publisher"}</Row>
      {panel.basket_slug ? (
        <Row label="Methodology">
          <Link
            href={`/basket/${panel.basket_slug}` as Route}
            data-testid="detail-methodology-link"
            className="text-accent underline-offset-4 hover:underline"
          >
            {panel.basket_name ?? "How this model picks its stocks"}
          </Link>
          {" · "}
          <Link
            href={`/basket/${panel.basket_slug}/constituents` as Route}
            className="text-accent underline-offset-4 hover:underline"
          >
            Current constituents
          </Link>
        </Row>
      ) : (
        <Row label="Methodology">
          The published model behind this portfolio is not linked yet, so its rules cannot be shown
          here.
        </Row>
      )}

      <div
        data-testid="detail-model-vs-actual"
        className="grid gap-3 rounded-lg border border-border/60 p-3 sm:grid-cols-2"
      >
        <div>
          <p className="eyebrow">On your money</p>
          <ReturnValue
            entry={fromFigure(summary.headline_return)}
            showReason
            data-testid="detail-actual-figure"
          />
        </div>
        <div className="sm:border-l sm:border-border/60 sm:pl-3">
          <p className="eyebrow">On the model itself</p>
          {summary.model_return ? (
            <ReturnValue
              entry={fromFigure(summary.model_return)}
              showReason
              data-testid="detail-model-figure"
            />
          ) : (
            <p className="text-[11px] leading-snug text-muted-foreground">
              {panel.publisher ?? "The publisher"} has not published a record for this model yet.
            </p>
          )}
        </div>
        <p className="text-[11px] leading-snug text-muted-foreground sm:col-span-2">
          Two measurements, never one. Yours starts when you subscribed and moves with what you
          actually hold and when you bought it; the model&rsquo;s is the publisher&rsquo;s own
          record of the model. They are not added, averaged or subtracted from one another.
        </p>
      </div>

      <Row label="When the model changes">
        {summary.status === "Rebalance due"
          ? "An update is waiting. Applying it builds an order plan from the weight difference, which you take to your broker."
          : "When the publisher changes the model, an update appears here and turns into an order plan you take to your broker."}
      </Row>
    </div>
  );
}

function RulesBody({ panel }: { panel: SourcePanel }) {
  return (
    <div className="flex flex-col gap-3" data-testid="detail-source-rules">
      <Row label="The rules">
        {panel.screen_public_id ? (
          <Link
            href={`/build/${panel.screen_public_id}` as Route}
            data-testid="detail-rules-link"
            className="text-accent underline-offset-4 hover:underline"
          >
            {panel.screen_name ?? "Open the rules behind this portfolio"}
          </Link>
        ) : (
          "The rules behind this portfolio are not linked to it yet."
        )}
      </Row>
      <Row label="Last run, entries and exits">
        Every entry and exit these rules produced is in the activity list below, newest first,
        with the date it happened on.
      </Row>
    </div>
  );
}

function HoldingGroupBody({ panel }: { panel: SourcePanel }) {
  const brokers = panel.brokers ?? [];
  return (
    <div className="flex flex-col gap-3" data-testid="detail-source-holding-group">
      <Row label="Included brokers">
        {brokers.length === 0
          ? "No broker account is connected to this portfolio yet."
          : brokers.map((broker) => broker.label).join(" · ")}
      </Row>
      <Row label="Grouped on">
        {panel.grouped_on ? (
          <span data-testid="detail-grouped-on">{formatTradeDate(panel.grouped_on)}</span>
        ) : (
          "The date these holdings were grouped was not recorded."
        )}
      </Row>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-0.5 sm:grid-cols-[10rem_1fr] sm:gap-3">
      <span className="eyebrow">{label}</span>
      <span className="text-sm text-muted-foreground">{children}</span>
    </div>
  );
}

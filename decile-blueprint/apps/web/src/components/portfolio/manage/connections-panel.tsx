"use client";

import { useMemo, useState } from "react";
import { ArrowUpRight } from "lucide-react";

import { Field, Notice, PanelHeading } from "@/components/portfolio/manage/panel-chrome";
import { WriteFailure, useWrite } from "@/components/portfolio/manage/write-state";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { actionById, successSentence, type ManageOutcome } from "@/lib/portfolio/manage";
import type { BrokerRef } from "@/lib/portfolio/organize";
import type { PortfolioRow } from "@/lib/portfolio/overview";

/**
 * Brokers and reconciliation — the two connections a portfolio has to the outside world.
 *
 * RE-ATTRIBUTING IS NOT MOVING
 * ----------------------------
 * `PATCH /portfolios/{id}` with a `broker_account_id` changes which account the **container**
 * names. It moves no share: holdings already written keep the account they were written at, and
 * the roll-up then reports the difference as a declaration conflict. The API's own docstring is
 * blunt about why — silently rewriting those rows would be the request claiming that stock had
 * moved between brokers, which is an assertion about the world that no HTTP call can make true.
 *
 * So the control says it. A person who reads "Broker account" on a portfolio form and changes it
 * expecting their shares to follow has been misled by the label, and the consequence is a
 * reconciliation question they did not know they were creating.
 *
 * CONNECTING A BROKER IS A DIFFERENT THING AND LIVES SOMEWHERE ELSE
 * ----------------------------------------------------------------
 * It is an OAuth round trip with a callback, a session and a 2FA screen that belongs to the
 * broker rather than to Baskfy. A drawer cannot host it, so this links to the page that does
 * rather than half-drawing a button that opens a page anyway.
 */

/** No choice made yet. Distinct from `""`, which is the real choice "spans several accounts". */
const UNCHOSEN = "UNCHOSEN";

export interface ConnectionsPanelProps {
  portfolios: readonly PortfolioRow[];
  /** Every broker account the user has connected, from the overview payload. */
  brokers: readonly BrokerRef[];
  /** `OverviewOut.open_reconciliation_count`. */
  openReconciliationCount: number;
  onReattribute?:
    | ((portfolioId: number, brokerAccountId: number | null) => Promise<ManageOutcome>)
    | undefined;
  onSaved: (message: string) => void;
}

export function ConnectionsPanel({
  portfolios,
  brokers,
  openReconciliationCount,
  onReattribute,
  onSaved,
}: ConnectionsPanelProps) {
  const action = actionById("broker");
  const reconciliation = actionById("reconciliation");
  const write = useWrite();

  const [portfolioId, setPortfolioId] = useState<number | null>(
    portfolios[0]?.portfolio_id ?? null,
  );
  const chosen = useMemo(
    () => portfolios.find((row) => row.portfolio_id === portfolioId) ?? null,
    [portfolios, portfolioId],
  );

  /**
   * `UNCHOSEN` until the user picks, and it is not a nicety.
   *
   * `PortfolioRowOut` carries the brokers whose holdings are *in* a portfolio; it does not carry
   * the `broker_account_id` the row is **declared** against. So this screen cannot show the
   * current attribution, and a select that defaulted to any option would be showing a value
   * nobody chose — press Re-attribute without touching it and you have silently changed the
   * declaration to whatever the control happened to say. It starts empty, the button is off
   * until a choice is made, and the panel says why.
   */
  const [accountId, setAccountId] = useState<string>(UNCHOSEN);

  async function commit(): Promise<void> {
    if (chosen === null || accountId === UNCHOSEN) return;
    const next = accountId === "" ? null : Number(accountId);
    await write.run(
      action,
      onReattribute === undefined ? undefined : () => onReattribute(chosen.portfolio_id, next),
      () => onSaved(successSentence(action, chosen.name)),
    );
  }

  return (
    <div className="space-y-5" data-testid="connections-panel">
      <section className="space-y-3">
        <PanelHeading title="Connect a broker">
          Holdings, cash and prices all arrive from a connected broker account. Signing in happens
          on the broker&rsquo;s own pages, so it is not something a drawer can host.
        </PanelHeading>
        <p className="text-xs text-muted-foreground">
          {brokers.length === 0
            ? "No broker account is connected yet, so every figure on the command centre is missing its source."
            : `${brokers.length} account${brokers.length === 1 ? "" : "s"} connected: ${brokers.map((broker) => broker.label).join(", ")}.`}
        </p>
        <a
          href="/brokers"
          className="inline-flex items-center gap-1 text-xs font-medium text-brand-strong underline-offset-4 hover:underline"
        >
          Open broker connections
          <ArrowUpRight aria-hidden="true" className="size-3" />
        </a>
      </section>

      <section className="space-y-3 border-t border-border pt-4">
        <PanelHeading title={action.title}>{action.blurb}</PanelHeading>

        <Notice testId="reattribute-notice">
          Changing this relabels the container and moves no share. Holdings already recorded keep
          the account they were recorded at, and the difference is then reported as a declaration
          conflict rather than silently rewritten — nothing here can make stock have moved between
          brokers.
        </Notice>

        {portfolios.length === 0 ? (
          <Notice tone="warning">There is no portfolio to attribute yet.</Notice>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Portfolio">
                {(id) => (
                  <Select
                    id={id}
                    value={portfolioId === null ? "" : String(portfolioId)}
                    onChange={(event) => {
                      setPortfolioId(event.target.value === "" ? null : Number(event.target.value));
                      write.clearFailure();
                    }}
                  >
                    {portfolios.map((row) => (
                      <option key={row.portfolio_id} value={String(row.portfolio_id)}>
                        {row.name}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
              <Field
                label="Attributed to"
                hint="This screen's payload does not carry the account a portfolio is currently declared against, so this starts empty rather than guessing at one. “Several accounts” is the honest answer for a portfolio whose holdings sit at more than one broker."
              >
                {(id) => (
                  <Select
                    id={id}
                    value={accountId}
                    onChange={(event) => {
                      setAccountId(event.target.value);
                      write.clearFailure();
                    }}
                  >
                    <option value={UNCHOSEN}>Choose an account</option>
                    <option value="">Several accounts — no single one</option>
                    {brokers.map((broker) => (
                      <option
                        key={broker.broker_account_id}
                        value={String(broker.broker_account_id)}
                      >
                        {broker.label}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            </div>

            <WriteFailure failure={write.failure} />

            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="primary"
                size="sm"
                disabled={
                  chosen === null ||
                  accountId === UNCHOSEN ||
                  write.saving ||
                  onReattribute === undefined
                }
                onClick={() => void commit()}
              >
                {write.saving ? "Saving…" : "Re-attribute"}
              </Button>
              {accountId === UNCHOSEN ? (
                <span className="text-xs text-muted-foreground" data-testid="broker-unchosen">
                  Choose which account this portfolio is attributed to first.
                </span>
              ) : onReattribute === undefined ? (
                <span className="text-xs text-muted-foreground" data-testid="broker-not-wired">
                  This page has not passed a save handler for attribution yet.
                </span>
              ) : null}
            </div>
          </>
        )}
      </section>

      <section className="space-y-3 border-t border-border pt-4">
        <PanelHeading title={reconciliation.title}>{reconciliation.blurb}</PanelHeading>
        <p className="text-xs text-muted-foreground" data-testid="reconciliation-count">
          {openReconciliationCount === 0
            ? "Nothing is waiting on an answer from you."
            : `${openReconciliationCount} question${openReconciliationCount === 1 ? "" : "s"} ${openReconciliationCount === 1 ? "is" : "are"} open. Each one freezes its holding's contribution to performance until it is answered, so the returns on the command centre are measured over less than everything you hold.`}
        </p>
        {/* Named honestly: there is an inbox, and there are no *settings*. A tolerance or an
            auto-resolve rule would need a route that does not exist, so none is drawn. */}
        <Notice testId="reconciliation-no-settings">
          There are no reconciliation settings to change — no tolerance, no auto-resolve rule.
          Baskfy reconciles every difference and asks about the ones it cannot decide; answering
          them is the only control, and it lives in the inbox.
        </Notice>
        <a
          href="/reconcile"
          className="inline-flex items-center gap-1 text-xs font-medium text-brand-strong underline-offset-4 hover:underline"
        >
          Open the reconciliation inbox
          <ArrowUpRight aria-hidden="true" className="size-3" />
        </a>
      </section>
    </div>
  );
}

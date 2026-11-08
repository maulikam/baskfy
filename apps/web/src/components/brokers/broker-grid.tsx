"use client";

import { useState, useTransition } from "react";

import { connectBrokerAction, syncHoldingsAction } from "@/app/actions/brokers";
import { Button } from "@/components/ui/button";
import type { Broker, BrokerGate } from "@/lib/brokers/fetch";
import { cn } from "@/lib/utils";

/**
 * The broker grid — M41 / P5.8, rewritten for what Baskfy actually integrates with (M55).
 *
 * Click a tile → the detail panel for that broker. **Two different integrations render here and
 * they are not variations of one thing:**
 *
 * **Kite Publisher** (Zerodha, `trading: "handoff"`). Nothing is connected. Baskfy prepares a
 * basket, the browser posts it to Kite, and the reader confirms it in whatever Zerodha session
 * they already have. No account link, no stored token, no holdings read back — Publisher is
 * one-way. This is the integration Baskfy uses, because Kite Connect is ₹2,000/month and
 * licensed for the app owner's own account, which is the wrong shape for a product other people
 * sign into.
 *
 * **A Connect-style OAuth adapter** (`oauth: "ready"`). Sign in at the broker, authorise a
 * holdings read, come back. Nothing is wired to this today; the panel still describes it because
 * the code path exists and a future broker may use it.
 *
 * The panel used to describe the second for every broker, including Zerodha — three green "Ready"
 * labels and a Connect button that could only ever return "app key is not configured".
 * `docs/DECISIONS-MERGE.md` M55.
 */

export interface BrokerGridProps {
  brokers: Broker[];
  gate: BrokerGate;
}

function capabilityLabel(value: string): string {
  if (value === "ready") return "Ready";
  if (value === "partner") return "Partner API";
  /* M55. Zerodha is Kite Publisher, and the old vocabulary could not say so: it rendered
     `holdings_sync` as a green "Ready" for a capability that does not exist on this integration
     and is not coming. "Planned" would have been the other lie. */
  if (value === "not_applicable") return "Not needed";
  if (value === "not_available") return "Not available";
  if (value === "handoff") return "You confirm in Kite";
  return "Planned";
}

/**
 * The one-word status under a tile.
 *
 * Reading `capabilities.oauth` was right while every row was a Connect adapter. For a hand-off
 * broker it renders "Not needed", which under a Zerodha tile is a true sentence answering a
 * question nobody asked. What a reader wants from a tile is what this broker *does*.
 */
function tileLabel(broker: Broker): string {
  if (broker.capabilities.trading === "handoff") return "Basket hand-off";
  return capabilityLabel(broker.capabilities.oauth);
}

export function BrokerGrid({ brokers, gate }: BrokerGridProps) {
  const [selectedId, setSelectedId] = useState<string | null>(brokers[0]?.id ?? null);
  const [message, setMessage] = useState<string | null>(null);
  const [syncNote, setSyncNote] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const [syncing, startSync] = useTransition();

  const selected = brokers.find((b) => b.id === selectedId) ?? null;

  function onConnect(brokerId: string) {
    setMessage(null);
    startTransition(async () => {
      const result = await connectBrokerAction(brokerId);
      if (result.oauth_available && result.redirect_url) {
        window.location.assign(result.redirect_url);
        return;
      }
      setMessage(result.reason || "Connect is not available for this broker yet.");
    });
  }

  function onSync(brokerId: string) {
    setSyncNote(null);
    startSync(async () => {
      const result = await syncHoldingsAction(brokerId);
      setSyncNote(
        result.persisted
          ? `${result.written} holding${result.written === 1 ? "" : "s"} synced.` +
              (result.unresolved.length
                ? ` ${result.unresolved.length} symbol(s) not recognised: ${result.unresolved.join(", ")}.`
                : "")
          : result.sync_note || result.note,
      );
    });
  }

  return (
    <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_20rem]">
      <ul
        className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-5"
        aria-label="Brokers you can connect"
      >
        {brokers.map((broker) => {
          const active = broker.id === selectedId;
          return (
            <li key={broker.id}>
              <button
                type="button"
                onClick={() => {
                  setSelectedId(broker.id);
                  setMessage(null);
                }}
                className={cn(
                  "flex w-full flex-col items-center gap-2 rounded-lg border bg-card px-3 py-4 text-center transition-colors",
                  active
                    ? "border-accent ring-1 ring-accent/40"
                    : "border-border/80 hover:border-foreground/25",
                )}
                aria-pressed={active}
              >
                <span
                  className="grid size-12 place-items-center rounded-md text-lg font-semibold text-white"
                  style={{ backgroundColor: broker.color }}
                  aria-hidden="true"
                >
                  {broker.mark}
                </span>
                <span className="text-sm font-medium text-foreground">{broker.short_name}</span>
                <span className="text-[11px] text-muted-foreground">
                  {broker.connected ? "Connected" : tileLabel(broker)}
                </span>
              </button>
            </li>
          );
        })}
      </ul>

      <aside className="flex flex-col gap-4 rounded-lg border border-border/80 bg-card p-4">
        {selected ? (
          <>
            <div className="flex items-center gap-3">
              <span
                className="grid size-10 place-items-center rounded-md text-sm font-semibold text-white"
                style={{ backgroundColor: selected.color }}
                aria-hidden="true"
              >
                {selected.mark}
              </span>
              <div>
                <h2 className="text-sm font-semibold">{selected.name}</h2>
                <p className="text-xs text-muted-foreground">{selected.api_name}</p>
              </div>
            </div>
            <p className="text-sm leading-relaxed text-muted-foreground">{selected.blurb}</p>
            {/*
              The steps of the flow this broker actually has. For Zerodha that is Kite Publisher:
              no sign-in here, no authorisation, nothing synced back. The previous three steps
              described a Kite Connect OAuth — "Authorize Baskfy to read holdings", "we sync the
              holdings into your portfolio" — which is a different product Baskfy does not use.
            */}
            <ol className="space-y-2 text-sm text-muted-foreground">
              {selected.capabilities.trading === "handoff" ? (
                <>
                  <li>
                    <span className="font-medium text-foreground">1.</span> Pick a basket and the
                    amount you want to put in.
                  </li>
                  <li>
                    <span className="font-medium text-foreground">2.</span> Baskfy works out the
                    share counts and opens them in {selected.short_name} as one basket.
                  </li>
                  <li>
                    <span className="font-medium text-foreground">3.</span> You review every line
                    and confirm there — nothing is placed from Baskfy.
                  </li>
                </>
              ) : (
                <>
                  <li>
                    <span className="font-medium text-foreground">1.</span> Open{" "}
                    {selected.short_name} and sign in.
                  </li>
                  <li>
                    <span className="font-medium text-foreground">2.</span> Authorize Baskfy to
                    read holdings (orders still need your confirm on a plan).
                  </li>
                  <li>
                    <span className="font-medium text-foreground">3.</span> Return here — we sync
                    the holdings into your portfolio.
                  </li>
                </>
              )}
            </ol>
            <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-muted-foreground">
              {/* "Login" is the wrong noun for an integration with no login. */}
              <dt>{selected.capabilities.oauth === "not_applicable" ? "Account link" : "Login"}</dt>
              <dd>{capabilityLabel(selected.capabilities.oauth)}</dd>
              <dt>Holdings sync</dt>
              <dd>{capabilityLabel(selected.capabilities.holdings_sync)}</dd>
              <dt>Trading</dt>
              <dd>{capabilityLabel(selected.capabilities.trading)}</dd>
            </dl>
            {/*
              The Connect button is shown only when this deployment can actually finish the login.

              Baskfy runs on Kite **Publisher**, the free product that hands a basket to the user's
              own Kite for them to confirm. Publisher issues no API secret, and the login this
              button starts ends at `session/token`, whose checksum needs one — so on a
              Publisher-only deployment the button could only ever produce an error about a missing
              credential. It did, twice, to Maulik, each time after he had pasted a perfectly good
              key. An affordance that cannot succeed is worse than no affordance: it teaches the
              reader that the product is broken rather than that this route is not the one they
              want. `NEEDS-MAULIK.md` §28.
            */}
            {/*
              CONNECTED IS A STATE, NOT JUST A TILE LABEL (M76).

              The tile has read `broker.connected` since M75 and correctly said "Connected". This
              panel did not: it rendered `Connect {short_name}` unconditionally, so the button
              Maulik actually clicks was unchanged by that fix and he reported the same defect
              twice. Reading the field in one of the two places it is displayed is not reading it.

              When connected, Connect stops being the primary action — re-running it would issue a
              fresh Kite token and invalidate the working session — and **Sync holdings** takes its
              place, because that is the step that was missing: the endpoint has persisted since
              M75 and nothing in the UI could call it.
            */}
            {gate.connect_configured && selected.connected ? (
              <>
                <p
                  data-testid="broker-connected"
                  className="rounded-md border border-border bg-muted/50 px-3 py-2 text-xs font-medium text-foreground"
                >
                  Connected to {selected.short_name}.
                </p>
                <Button
                  type="button"
                  disabled={syncing}
                  onClick={() => onSync(selected.id)}
                  className="w-full"
                >
                  {syncing ? "Syncing…" : "Sync holdings"}
                </Button>
                {syncNote ? (
                  <p
                    data-testid="sync-note"
                    className="rounded-md border border-border bg-muted/50 px-3 py-2 text-xs leading-relaxed text-muted-foreground"
                  >
                    {syncNote}
                  </p>
                ) : null}
                <button
                  type="button"
                  disabled={pending}
                  onClick={() => onConnect(selected.id)}
                  className="w-full text-xs text-muted-foreground underline underline-offset-2"
                >
                  {pending ? "Starting…" : `Reconnect ${selected.short_name}`}
                </button>
                {message ? (
                  <p className="rounded-md border border-border bg-muted/50 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
                    {message}
                  </p>
                ) : null}
              </>
            ) : gate.connect_configured ? (
              <>
                {/*
                  Say WHY, when we know why (M79). Kite invalidates an access token at the start of
                  the next trading day, so a login that worked last night is gone by morning and
                  the button silently becomes "Connect" again. Told plainly, that is a daily
                  routine; left unexplained it reads as the connect having never worked — which is
                  exactly the report that started this.
                */}
                {selected.connection_status === "expired" ? (
                  <p
                    data-testid="broker-expired"
                    className="rounded-md border border-border bg-muted/50 px-3 py-2 text-xs leading-relaxed text-muted-foreground"
                  >
                    Your {selected.short_name} session expired. Kite ends a session at the start of
                    each trading day, so this needs connecting again to sync holdings.
                  </p>
                ) : null}
                <Button
                  type="button"
                  disabled={pending}
                  onClick={() => onConnect(selected.id)}
                  className="w-full"
                >
                  {pending
                    ? "Starting…"
                    : selected.connection_status === "expired"
                      ? `Reconnect ${selected.short_name}`
                      : `Connect ${selected.short_name}`}
                </Button>
                {message ? (
                  <p className="rounded-md border border-border bg-muted/50 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
                    {message}
                  </p>
                ) : null}
                <p className="text-xs leading-relaxed text-muted-foreground">
                  After connect, holdings sync reads quantity + T1 + collateral. Orders still
                  require an explicit confirm on a plan — this page never executes.
                </p>
              </>
            ) : (
              /*
                Deliberately nothing here.

                This was a panel repeating "You don't need to connect Zerodha to invest", how the
                basket hand-off works, and two links. Every word of it was true and it was in the
                wrong place: the page's own banner already opens with "Baskfy sends baskets to your
                broker; it never connects to your account", so the detail panel said it a second
                time, at greater length, directly under a heading naming the broker. Read in
                sequence it stops sounding like a design and starts sounding like an apology for a
                missing feature (Maulik, 29 Aug 2026).

                What a reader needs from this panel when there is no login to offer is already
                above it: the capability rows say Account link "Not needed", Holdings sync "Not
                available", Trading "You confirm in Kite". `docs/DECISIONS-MERGE.md` M57.
              */
              null
            )}
          </>
        ) : (
          <p className="text-sm text-muted-foreground">Pick a broker to see how connect works.</p>
        )}
      </aside>
    </div>
  );
}

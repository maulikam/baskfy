"use client";

import Link from "next/link";
import { useState, useTransition } from "react";

import { connectBrokerAction } from "@/app/actions/brokers";
import { Button } from "@/components/ui/button";
import type { Broker, BrokerGate } from "@/lib/brokers/fetch";
import { cn } from "@/lib/utils";

/**
 * The connect grid — M41 / P5.8.
 *
 * Click a tile → detail with the three-step flow. Connect calls the API; when the D3 gate is
 * open and the adapter is wired, the browser redirects to the broker. Holdings sync never
 * places an order from the web.
 */

export interface BrokerGridProps {
  brokers: Broker[];
  gate: BrokerGate;
}

function capabilityLabel(value: string): string {
  if (value === "ready") return "Ready";
  if (value === "partner") return "Partner API";
  return "Planned";
}

export function BrokerGrid({ brokers, gate }: BrokerGridProps) {
  const [selectedId, setSelectedId] = useState<string | null>(brokers[0]?.id ?? null);
  const [message, setMessage] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

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
                  {broker.connected ? "Connected" : capabilityLabel(broker.capabilities.oauth)}
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
            <ol className="space-y-2 text-sm text-muted-foreground">
              <li>
                <span className="font-medium text-foreground">1.</span> Open {selected.short_name}{" "}
                and sign in.
              </li>
              <li>
                <span className="font-medium text-foreground">2.</span> Authorize Baskfy to read
                holdings (orders still need your confirm on a plan).
              </li>
              <li>
                <span className="font-medium text-foreground">3.</span> Return here — we sync the
                holdings into your portfolio.
              </li>
            </ol>
            <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <dt>Login</dt>
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
            {gate.connect_configured ? (
              <>
                <Button
                  type="button"
                  disabled={pending}
                  onClick={() => onConnect(selected.id)}
                  className="w-full"
                >
                  {pending ? "Starting…" : `Connect ${selected.short_name}`}
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
              <div className="flex flex-col gap-2 rounded-md border border-border bg-muted/50 px-3 py-3">
                <p className="text-xs font-medium text-foreground">
                  You don&apos;t need to connect {selected.short_name} to invest.
                </p>
                <p className="text-xs leading-relaxed text-muted-foreground">
                  Open any basket, choose an amount, and Baskfy hands it to your own Kite as a
                  ready-made order basket. You review every line and confirm it there — Baskfy
                  never places an order and never holds your broker credentials.
                </p>
                <p className="text-xs leading-relaxed text-muted-foreground">
                  <Link href="/discover" className="text-accent underline-offset-4 hover:underline">
                    Browse baskets
                  </Link>
                  {" · "}
                  <Link
                    href="/portfolio/portfolios"
                    className="text-accent underline-offset-4 hover:underline"
                  >
                    Import a holdings CSV
                  </Link>{" "}
                  to track what you already own.
                </p>
              </div>
            )}
          </>
        ) : (
          <p className="text-sm text-muted-foreground">Pick a broker to see how connect works.</p>
        )}
      </aside>
    </div>
  );
}

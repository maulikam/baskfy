"use client";

import { useState, useTransition } from "react";
import type { AdminUserDetailOut, AdminUserOut } from "@baskfy/api-client";

import {
  clearEntitlementOverride,
  setEntitlementOverride,
  type AdminActionResult,
} from "@/app/actions/admin";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { formatDateTimeIST } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * PROMPTS.md Prompt 17 §4's "user lookup" and "entitlement override", together — because they are
 * one task. Nobody looks an account up for its own sake; they look it up because a customer says
 * something is wrong, and the next thing they need is the ability to fix it.
 *
 * What the panel shows is the account's **effective** entitlements — plan plus overrides, resolved
 * by `baskfy_api.entitlements.entitlements_for`, which is the same call every gated endpoint
 * makes. So this page cannot disagree with enforcement, which is the failure an admin surface
 * makes most easily and most expensively.
 *
 * The search and the detail read are client fetches against `/admin/*` through the *web app's*
 * route, not the API: the staff bearer token stays on the server (see `app/actions/admin.ts`).
 * That is why they go through a search action rather than `browserApi()`.
 */

/** docs/07 §Entitlements' six booleans, plus the one number. `admin.OVERRIDABLE` is the server's copy. */
const FEATURES = [
  "screener",
  "export_csv",
  "custom_columns",
  "historical_ranks",
  "backtests",
  "api_access",
  "max_screens",
] as const;

/** A support grant with no end is how a comp account is created by accident. */
function defaultExpiry(): string {
  const when = new Date();
  when.setUTCMonth(when.getUTCMonth() + 1);
  return when.toISOString().slice(0, 10);
}

export interface UserLookupProps {
  query: string;
  results: AdminUserOut[];
  selected: AdminUserDetailOut | null;
}

export function UserLookup({ query, results, selected }: UserLookupProps) {
  const [result, setResult] = useState<AdminActionResult | null>(null);
  const [pending, startTransition] = useTransition();
  const [feature, setFeature] = useState<string>("export_csv");
  const [effect, setEffect] = useState<"grant" | "revoke">("grant");
  const [reason, setReason] = useState("");
  const [value, setValue] = useState("");
  const [expiresAt, setExpiresAt] = useState(defaultExpiry);

  return (
    <div className="space-y-6">
      <form method="get" className="flex flex-wrap items-end gap-3">
        <div className="min-w-64 flex-1">
          <Label htmlFor="admin-user-q">Email fragment or public id</Label>
          <Input
            id="admin-user-q"
            name="q"
            defaultValue={query}
            placeholder="them@example.com"
            autoComplete="off"
          />
        </div>
        <Button type="submit">Search</Button>
      </form>

      {results.length > 0 ? (
        <div className="overflow-x-auto rounded border border-border">
          <table className="w-full border-collapse text-sm">
            <caption className="sr-only">Accounts matching {query}</caption>
            <thead>
              <tr className="border-b border-border bg-muted/40 text-left">
                <th scope="col" className="px-3 py-2 font-medium">Email</th>
                <th scope="col" className="px-3 py-2 font-medium">public_id</th>
                <th scope="col" className="px-3 py-2 font-medium">Created</th>
                <th scope="col" className="px-3 py-2 font-medium">State</th>
              </tr>
            </thead>
            <tbody>
              {results.map((user) => (
                <tr key={user.public_id} className="border-b border-border last:border-0">
                  <td className="px-3 py-2">
                    <a
                      className="underline underline-offset-2"
                      href={`/admin/users?q=${encodeURIComponent(query)}&id=${user.public_id}`}
                    >
                      {user.email}
                    </a>
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">{user.public_id}</td>
                  <td className="px-3 py-2">{formatDateTimeIST(user.created_at)}</td>
                  <td className="px-3 py-2 text-xs">
                    {/* Deleted accounts are shown, not hidden: "where did their account go" is
                        exactly the question that brings someone to this page. */}
                    {user.deleted_at ? (
                      <span className="text-negative">erasure pending</span>
                    ) : user.email_verified ? (
                      "verified"
                    ) : (
                      <span className="text-muted-foreground">unverified</span>
                    )}
                    {user.is_staff ? <span className="ml-2 font-medium">staff</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : query ? (
        <p className="text-sm text-muted-foreground">Nothing matched {query}.</p>
      ) : null}

      {selected ? (
        <section className="space-y-4 rounded border border-border p-4">
          <header>
            <h2 className="text-base font-medium">{selected.user.email}</h2>
            <p className="text-sm text-muted-foreground">
              {selected.plan_code ? (
                <>
                  Plan <code className="font-mono">{selected.plan_code}</code> (
                  {selected.subscription_status ?? "no subscription"})
                </>
              ) : (
                "No subscription has ever existed for this account."
              )}{" "}
              · {selected.screen_count} saved screens
            </p>
          </header>

          <div>
            <h3 className="mb-2 text-sm font-medium">Effective entitlements</h3>
            <ul className="flex flex-wrap gap-2 text-xs">
              {Object.entries(selected.entitlements).map(([key, granted]) => (
                <li
                  key={key}
                  className={cn(
                    "rounded px-2 py-1 font-mono",
                    granted === true
                      ? "bg-positive/10 text-positive"
                      : granted === false
                        ? "bg-muted text-muted-foreground"
                        : "bg-accent text-accent-foreground",
                  )}
                >
                  {key}: {String(granted)}
                </li>
              ))}
            </ul>
            <p className="mt-2 text-xs text-muted-foreground">
              Resolved by the same call every gated endpoint makes, so this is what the account
              actually gets — plan first, then overrides.
            </p>
          </div>

          {selected.overrides.length > 0 ? (
            <div>
              <h3 className="mb-2 text-sm font-medium">Overrides in place</h3>
              <ul className="space-y-2 text-sm">
                {selected.overrides.map((override) => (
                  <li
                    key={override.feature}
                    className="flex flex-wrap items-center gap-3 rounded border border-border px-3 py-2"
                  >
                    <code className="font-mono text-xs">
                      {override.effect} {override.feature}
                      {override.value === null || override.value === undefined
                        ? ""
                        : ` = ${override.value}`}
                    </code>
                    <span className="text-xs text-muted-foreground">
                      {override.reason} · by {override.granted_by ?? "unknown"} ·{" "}
                      {override.expires_at
                        ? `expires ${formatDateTimeIST(override.expires_at)}`
                        : "no expiry"}
                      {override.active ? "" : " · EXPIRED"}
                    </span>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="ml-auto"
                      disabled={pending}
                      onClick={() =>
                        startTransition(async () => {
                          setResult(
                            await clearEntitlementOverride(
                              selected.user.public_id,
                              override.feature,
                            ),
                          );
                        })
                      }
                    >
                      Remove
                    </Button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="space-y-3 border-t border-border pt-4">
            <h3 className="text-sm font-medium">Add or replace an override</h3>
            <div className="flex flex-wrap gap-3">
              <div>
                <Label htmlFor="override-feature">Feature</Label>
                <select
                  id="override-feature"
                  className="h-9 rounded border border-input bg-transparent px-2 text-sm"
                  value={feature}
                  onChange={(event) => setFeature(event.target.value)}
                >
                  {FEATURES.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <Label htmlFor="override-effect">Effect</Label>
                <select
                  id="override-effect"
                  className="h-9 rounded border border-input bg-transparent px-2 text-sm"
                  value={effect}
                  onChange={(event) => setEffect(event.target.value === "revoke" ? "revoke" : "grant")}
                >
                  <option value="grant">grant</option>
                  <option value="revoke">revoke</option>
                </select>
              </div>
              {feature === "max_screens" ? (
                <div className="w-32">
                  <Label htmlFor="override-value">Value</Label>
                  <Input
                    id="override-value"
                    inputMode="numeric"
                    value={value}
                    onChange={(event) => setValue(event.target.value)}
                  />
                </div>
              ) : null}
              <div>
                <Label htmlFor="override-expiry">Expires</Label>
                <Input
                  id="override-expiry"
                  type="date"
                  value={expiresAt}
                  onChange={(event) => setExpiresAt(event.target.value)}
                />
              </div>
              <div className="min-w-64 flex-1">
                <Label htmlFor="override-reason">Reason (stored, with your name)</Label>
                <Input
                  id="override-reason"
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder="webhook evt_xxx not delivered; paid 2026-08-21"
                />
              </div>
            </div>
            <Button
              disabled={pending}
              onClick={() =>
                startTransition(async () => {
                  setResult(
                    await setEntitlementOverride({
                      publicId: selected.user.public_id,
                      feature,
                      effect,
                      reason,
                      value: feature === "max_screens" && value ? Number(value) : null,
                      expiresAt: expiresAt || null,
                    }),
                  );
                })
              }
            >
              Save override
            </Button>
          </div>

          {result ? (
            <p
              role="status"
              className={cn(
                "rounded border px-3 py-2 text-sm",
                result.ok
                  ? "border-positive/30 bg-positive/10 text-positive"
                  : "border-negative/30 bg-negative/10 text-negative",
              )}
            >
              {result.message}
            </p>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}

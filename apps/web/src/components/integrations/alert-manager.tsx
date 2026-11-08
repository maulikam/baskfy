"use client";

import type { ScreenAlertOut } from "@baskfy/api-client";
import { useState, useTransition } from "react";

import {
  deleteAlert,
  setAlertActive,
  setAlertDigest,
  subscribeScreen,
  type ActionResult,
} from "@/app/actions/integrations";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function schedule(alert: ScreenAlertOut): string {
  if (alert.frequency === "daily") return "Every publishing day";
  const day = alert.weekday === null || alert.weekday === undefined ? 4 : alert.weekday;
  return `Weekly, on ${WEEKDAYS[day] ?? "Friday"}`;
}

/** Only what the subscribe picker needs — not the whole `ScreenOut`. */
export interface SubscribableScreen {
  public_id: string;
  name: string;
}

/**
 * Subscribe a screen, and manage the alerts already on — PROMPTS.md Prompt 20 §3.
 *
 * "Digest" is the preference the prompt asks for, worded as what it actually does rather than as
 * the column name: on, this screen travels in one combined email with every other digest alert;
 * off, it gets its own message. Pausing is kept separate from deleting because the delivery
 * history is the answer to "why did I get that email", and deleting takes it with it.
 *
 * A screen that already has an alert is filtered out of the picker rather than offered and then
 * refused: the API answers 400 for a second subscription, and a dropdown that lets you pick
 * something guaranteed to fail is a worse experience than one that does not offer it.
 */
export function AlertManager({
  alerts,
  screens,
}: {
  alerts: readonly ScreenAlertOut[];
  screens: readonly SubscribableScreen[];
}) {
  const [result, setResult] = useState<ActionResult | null>(null);
  const [pending, startTransition] = useTransition();
  const subscribed = new Set(alerts.map((alert) => alert.screen_public_id));
  const available = screens.filter((screen) => !subscribed.has(screen.public_id));
  const [choice, setChoice] = useState("");
  const [frequency, setFrequency] = useState<"daily" | "weekly">("daily");
  const [digest, setDigest] = useState(false);

  const run = (action: () => Promise<ActionResult>) =>
    startTransition(async () => {
      setResult(await action());
    });

  return (
    <div className="space-y-6">
      <section className="space-y-3">
        <div className="flex flex-wrap items-end gap-3">
          <div className="w-72">
            <Label htmlFor="alert-screen">Screen</Label>
            <select
              id="alert-screen"
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              value={choice}
              onChange={(event) => setChoice(event.target.value)}
              disabled={available.length === 0}
            >
              <option value="">
                {available.length === 0 ? "Every screen is already subscribed" : "Pick a screen…"}
              </option>
              {available.map((screen) => (
                <option key={screen.public_id} value={screen.public_id}>
                  {screen.name}
                </option>
              ))}
            </select>
          </div>
          <div className="w-44">
            <Label htmlFor="alert-frequency">How often</Label>
            <select
              id="alert-frequency"
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              value={frequency}
              onChange={(event) =>
                setFrequency(event.target.value === "weekly" ? "weekly" : "daily")
              }
            >
              <option value="daily">After every publish</option>
              <option value="weekly">Weekly, on Friday</option>
            </select>
          </div>
          <label className="flex h-9 items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={digest}
              onChange={(event) => setDigest(event.target.checked)}
            />
            Fold into one digest
          </label>
          <Button
            disabled={pending || !choice}
            onClick={() =>
              startTransition(async () => {
                const created = await subscribeScreen(choice, frequency, digest);
                setResult(created);
                if (created.ok) setChoice("");
              })
            }
          >
            Subscribe
          </Button>
        </div>
      </section>

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

      {alerts.length === 0 ? (
        <p className="rounded-md border border-border bg-muted/50 p-4 text-sm text-muted-foreground">
          No alerts yet. Subscribe a screen above to get an email when names enter or leave it.
        </p>
      ) : (
      <ul className="divide-y divide-border rounded-md border border-border">
        {alerts.map((alert) => (
          <li key={alert.public_id} className="flex flex-wrap items-center gap-4 p-4">
            <div className="min-w-56 flex-1">
              <p className="font-medium">{alert.screen_name}</p>
              <p className="text-xs text-muted-foreground">
                {schedule(alert)}
                {alert.top_n ? ` · top ${alert.top_n}` : ""}
                {alert.digest ? " · in the combined digest" : " · its own email"}
                {alert.last_sent_at ? ` · last sent ${alert.last_sent_at.slice(0, 10)}` : ""}
              </p>
            </div>
            <span
              className={cn(
                "text-xs",
                alert.is_active ? "text-positive" : "text-muted-foreground",
              )}
            >
              {alert.is_active ? "on" : "paused"}
            </span>
            <span className="flex gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={pending}
                onClick={() => run(() => setAlertActive(alert.public_id, !alert.is_active))}
              >
                {alert.is_active ? "Pause" : "Resume"}
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={pending}
                onClick={() => run(() => setAlertDigest(alert.public_id, !alert.digest))}
              >
                {alert.digest ? "Send separately" : "Add to digest"}
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={pending}
                onClick={() => run(() => deleteAlert(alert.public_id))}
              >
                Delete
              </Button>
            </span>
          </li>
        ))}
      </ul>
      )}
    </div>
  );
}

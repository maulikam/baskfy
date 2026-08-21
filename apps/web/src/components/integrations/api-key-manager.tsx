"use client";

import type { ApiKeyOut } from "@decile/api-client";
import { useState, useTransition } from "react";

import {
  createApiKey,
  revokeApiKey,
  rotateApiKey,
  type ActionResult,
  type CreatedKeyResult,
} from "@/app/actions/integrations";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/**
 * The API-key list, its lifecycle buttons and the usage dashboard — Prompt 20 deliverable 1.
 *
 * **The secret is shown exactly once**, in the panel below the form, and nothing on this page ever
 * fetches it again — the API cannot return it a second time, because it stores a SHA-256 digest
 * and not the value (`decile_api.api_keys`). The panel therefore says so in as many words rather
 * than letting a user assume they can come back for it.
 *
 * Revocation is immediate and the copy says that too: Prompt 20's first acceptance criterion is
 * that a revoked key is refused within one second, and a UI that implied a grace period would be
 * describing a system we deliberately did not build.
 */
export function ApiKeyManager({ keys }: { keys: readonly ApiKeyOut[] }) {
  const [name, setName] = useState("");
  const [issued, setIssued] = useState<CreatedKeyResult | null>(null);
  const [result, setResult] = useState<ActionResult | null>(null);
  const [pending, startTransition] = useTransition();

  const run = (action: () => Promise<ActionResult>) =>
    startTransition(async () => {
      setResult(await action());
    });

  return (
    <div className="space-y-6">
      <section className="space-y-3">
        <div className="flex flex-wrap items-end gap-3">
          <div className="w-72">
            <Label htmlFor="key-name">Name this key</Label>
            <Input
              id="key-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="research-notebook"
              autoComplete="off"
            />
          </div>
          <Button
            disabled={pending}
            onClick={() =>
              startTransition(async () => {
                const created = await createApiKey(name);
                setIssued(created.ok ? created : null);
                setResult(created);
                if (created.ok) setName("");
              })
            }
          >
            Create key
          </Button>
        </div>
        <p className="max-w-prose text-xs text-muted-foreground">
          Keys are read-only. They carry no ability to save a screen, run a backtest, change a plan
          or create another key — a leaked key reads, and nothing else.
        </p>
      </section>

      {issued?.secret ? (
        <section className="space-y-2 rounded-md border border-accent/40 bg-accent/5 p-4">
          <h2 className="text-sm font-medium">Copy this now</h2>
          <code className="block break-all rounded bg-background px-3 py-2 font-mono text-sm">
            {issued.secret}
          </code>
          <p className="text-xs text-muted-foreground">
            This is the only time it exists. We store a hash, not the key, so nobody here can read
            it back to you — a lost key is rotated, not recovered.
          </p>
          <Button variant="outline" size="sm" onClick={() => setIssued(null)}>
            I have copied it
          </Button>
        </section>
      ) : null}

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

      {keys.length === 0 ? (
        <p className="rounded-md border border-border bg-muted/50 p-4 text-sm text-muted-foreground">
          No keys yet.
        </p>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-muted-foreground">
            <tr>
              <th className="py-2 pr-4 font-medium">Key</th>
              <th className="py-2 pr-4 font-medium">Name</th>
              <th className="py-2 pr-4 text-right font-medium">Requests (30d)</th>
              <th className="py-2 pr-4 text-right font-medium">Throttled</th>
              <th className="py-2 pr-4 font-medium">Last used</th>
              <th className="py-2 pr-4 font-medium">Status</th>
              <th className="py-2 font-medium">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {keys.map((key) => (
              <tr key={key.public_id} className="border-t border-border">
                <td className="py-2 pr-4 font-mono text-xs">{key.display}…</td>
                <td className="py-2 pr-4">{key.name}</td>
                <td className="py-2 pr-4 text-right tabular-nums">{key.requests_30d}</td>
                <td
                  className={cn(
                    "py-2 pr-4 text-right tabular-nums",
                    key.throttled_30d > 0 ? "text-negative" : "text-muted-foreground",
                  )}
                >
                  {key.throttled_30d}
                </td>
                <td className="py-2 pr-4 text-muted-foreground">
                  {key.last_used_at ? key.last_used_at.slice(0, 10) : "never"}
                </td>
                <td className="py-2 pr-4">
                  {key.active ? (
                    <span className="text-positive">active</span>
                  ) : (
                    <span className="text-muted-foreground">
                      revoked{key.revoked_reason ? ` (${key.revoked_reason})` : ""}
                    </span>
                  )}
                </td>
                <td className="py-2 text-right">
                  {key.active ? (
                    <span className="flex justify-end gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={pending}
                        onClick={() =>
                          startTransition(async () => {
                            const rotated = await rotateApiKey(key.public_id);
                            setIssued(rotated.ok ? rotated : null);
                            setResult(rotated);
                          })
                        }
                      >
                        Rotate
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={pending}
                        onClick={() => run(() => revokeApiKey(key.public_id, "revoked by owner"))}
                      >
                        Revoke
                      </Button>
                    </span>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

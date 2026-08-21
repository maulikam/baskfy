"use client";

import { useActionState, useId, useState } from "react";

import { deleteAccount } from "@/app/actions/account";
import { FormStatus } from "@/components/auth/form-status";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * DPDP export and erasure — docs/11 §Compliance, Prompt 12 §5.
 *
 * Export is a plain link to a route handler that proxies `GET /me/export` with the session's
 * bearer token; erasure is behind a disclosure *and* asks for the address back, because it is the
 * one irreversible action in the product. The seven-day window is stated before the button, not
 * after it.
 */
export function DangerZone({ email }: { email: string }) {
  const [result, action, pending] = useActionState(deleteAccount, null);
  const [open, setOpen] = useState(false);
  const confirmId = useId();

  return (
    <section
      aria-labelledby="danger-heading"
      className="flex flex-col gap-3 rounded-lg border border-negative/30 p-4"
    >
      <h2 id="danger-heading" className="text-sm font-semibold">
        Your data
      </h2>

      <p className="text-sm text-muted-foreground">
        You can download everything we hold about this account, or ask for it to be erased.
      </p>

      <a
        href="/api/account/export"
        className="w-fit rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted"
        data-testid="export-my-data"
      >
        Download my data (JSON)
      </a>

      {open ? (
        <form action={action} className="flex flex-col gap-3 border-t border-border pt-3">
          <p className="text-sm">
            Your account will be deactivated straight away and permanently erased seven days
            later. Signing in again before then cancels it.
          </p>
          <Label htmlFor={confirmId}>Type {email} to confirm</Label>
          <Input id={confirmId} name="email" type="email" autoComplete="off" required />
          <FormStatus result={result} />
          <div className="flex gap-2">
            <Button type="submit" variant="destructive" disabled={pending}>
              {pending ? "Scheduling…" : "Delete my account"}
            </Button>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
          </div>
        </form>
      ) : (
        <Button
          type="button"
          variant="outline"
          className="w-fit"
          onClick={() => setOpen(true)}
          data-testid="open-delete-account"
        >
          Delete my account
        </Button>
      )}
    </section>
  );
}

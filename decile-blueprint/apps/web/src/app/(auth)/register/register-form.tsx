"use client";

import Link from "next/link";
import { useActionState, useId } from "react";

import { register } from "@/app/actions/auth";
import { FormStatus } from "@/components/auth/form-status";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * The registration form.
 *
 * On success the API answers 202 with a deliberately uninformative sentence — it says the same
 * thing whether or not the address already has an account (docs/11's PII inventory makes the
 * customer list PII). So this form does *not* redirect on success: there is nothing to redirect
 * to until the address is confirmed, and a redirect would imply an account was created, which is
 * precisely what the endpoint declines to reveal.
 */
export function RegisterForm() {
  const [result, action, pending] = useActionState(register, null);
  const nameId = useId();
  const emailId = useId();
  const passwordId = useId();

  if (result?.ok) {
    return (
      <div className="space-y-3 rounded-md border border-border bg-card p-4">
        <p className="text-sm">{result.message}</p>
        <p className="text-sm text-muted-foreground">
          Follow the link in that email to confirm your address, then{" "}
          <Link href="/login" className="text-accent underline-offset-4 hover:underline">
            sign in
          </Link>
          .
        </p>
      </div>
    );
  }

  return (
    <form action={action} className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor={nameId}>Name</Label>
        <Input id={nameId} name="name" autoComplete="name" />
      </div>

      <div className="space-y-2">
        <Label htmlFor={emailId}>Email</Label>
        <Input id={emailId} name="email" type="email" autoComplete="email" required />
      </div>

      <div className="space-y-2">
        <Label htmlFor={passwordId}>
          Password <span className="text-muted-foreground">(optional)</span>
        </Label>
        <Input
          id={passwordId}
          name="password"
          type="password"
          autoComplete="new-password"
          minLength={8}
        />
        <p className="text-xs text-muted-foreground">
          At least 8 characters. Leave it blank to sign in with a one-time code instead.
        </p>
      </div>

      <div className="space-y-2">
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" name="accept_terms" required className="mt-1" />
          {/*
            Not links yet. docs/11 §Compliance requires Terms, a Privacy Policy and a Refund
            Policy "before taking a single payment", and those pages are Prompt 18's; linking to
            a 404 from a consent checkbox would be worse than naming them plainly. The consent
            record stores the document version regardless, so consent to the published text is a
            new record rather than a silent re-use of this one. `docs/12a` §10.
          */}
          <span>I accept the Terms and the Privacy Policy.</span>
        </label>
        <label className="flex items-start gap-2 text-sm text-muted-foreground">
          <input type="checkbox" name="accept_marketing" className="mt-1" />
          <span>Email me occasional product updates. (Optional.)</span>
        </label>
      </div>

      <FormStatus result={result} />

      <Button type="submit" variant="primary" className="w-full" disabled={pending}>
        {pending ? "Creating account…" : "Create account"}
      </Button>
    </form>
  );
}

"use client";

import Link from "next/link";
import { useActionState, useId } from "react";

import { chooseNewPassword } from "@/app/actions/auth";
import { FormStatus } from "@/components/auth/form-status";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function ResetPasswordForm({ token }: { token: string }) {
  const [result, action, pending] = useActionState(chooseNewPassword, null);
  const passwordId = useId();

  if (result?.ok) {
    return (
      <div className="space-y-3 rounded-md border border-border bg-card p-4">
        <p className="text-sm">{result.message}</p>
        <p className="text-sm">
          <Link href="/login" className="text-accent underline-offset-4 hover:underline">
            Sign in with your new password
          </Link>
        </p>
      </div>
    );
  }

  return (
    <form action={action} className="space-y-3">
      <input type="hidden" name="token" value={token} />
      <Label htmlFor={passwordId}>New password</Label>
      <Input
        id={passwordId}
        name="password"
        type="password"
        autoComplete="new-password"
        minLength={8}
        required
      />
      <FormStatus result={result} />
      <Button type="submit" variant="primary" className="w-full" disabled={pending}>
        {pending ? "Saving…" : "Set new password"}
      </Button>
    </form>
  );
}

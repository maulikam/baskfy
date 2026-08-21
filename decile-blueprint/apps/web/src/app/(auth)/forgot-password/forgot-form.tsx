"use client";

import { useActionState, useId } from "react";

import { sendResetLink } from "@/app/actions/auth";
import { FormStatus } from "@/components/auth/form-status";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function ForgotPasswordForm() {
  const [result, action, pending] = useActionState(sendResetLink, null);
  const emailId = useId();

  return (
    <form action={action} className="space-y-3">
      <Label htmlFor={emailId}>Email</Label>
      <Input id={emailId} name="email" type="email" autoComplete="email" required />
      <FormStatus result={result} />
      <Button type="submit" variant="primary" className="w-full" disabled={pending}>
        {pending ? "Sending…" : "Send reset link"}
      </Button>
    </form>
  );
}

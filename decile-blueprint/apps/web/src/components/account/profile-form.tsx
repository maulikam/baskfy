"use client";

import { useActionState, useId } from "react";

import { updateProfile } from "@/app/actions/account";
import { FormStatus } from "@/components/auth/form-status";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * The display name is the only editable field. Changing the email address is an identity change:
 * it needs the new address proved before it takes effect, which is a different flow and a
 * different endpoint. `docs/12a` §8.
 */
export function ProfileForm({
  name,
  email,
  verified,
}: {
  name: string;
  email: string;
  verified: boolean;
}) {
  const [result, action, pending] = useActionState(updateProfile, null);
  const nameId = useId();
  const emailId = useId();

  return (
    <form action={action} className="flex flex-col gap-3">
      <div className="flex flex-col gap-2">
        <Label htmlFor={nameId}>Name</Label>
        <Input id={nameId} name="name" defaultValue={name} autoComplete="name" required />
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor={emailId}>Email</Label>
        <div className="flex items-center gap-2">
          <Input id={emailId} value={email} readOnly disabled className="max-w-sm" />
          <Badge variant={verified ? "positive" : "warning"}>
            {verified ? "Verified" : "Unverified"}
          </Badge>
        </div>
        <p className="text-xs text-muted-foreground">
          Changing your email address needs the new one confirmed first, which is not built yet.
        </p>
      </div>

      <FormStatus result={result} />

      <Button type="submit" variant="primary" className="w-fit" disabled={pending}>
        {pending ? "Saving…" : "Save"}
      </Button>
    </form>
  );
}

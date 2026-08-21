"use client";

import { useActionState, useId } from "react";

import { changePassword } from "@/app/actions/account";
import { FormStatus } from "@/components/auth/form-status";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function ChangePasswordForm({ hasPassword }: { hasPassword: boolean }) {
  const [result, action, pending] = useActionState(changePassword, null);
  const currentId = useId();
  const nextId = useId();
  const confirmId = useId();

  return (
    <form action={action} className="flex flex-col gap-3">
      {hasPassword ? (
        <div className="flex flex-col gap-2">
          <Label htmlFor={currentId}>Current password</Label>
          <Input
            id={currentId}
            name="current_password"
            type="password"
            autoComplete="current-password"
            required
          />
        </div>
      ) : null}

      <div className="flex flex-col gap-2">
        <Label htmlFor={nextId}>New password</Label>
        <Input
          id={nextId}
          name="new_password"
          type="password"
          autoComplete="new-password"
          minLength={8}
          required
        />
        <p className="text-xs text-muted-foreground">At least 8 characters.</p>
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor={confirmId}>Confirm new password</Label>
        <Input
          id={confirmId}
          name="confirm_password"
          type="password"
          autoComplete="new-password"
          minLength={8}
          required
        />
      </div>

      <FormStatus result={result} />

      <Button type="submit" variant="primary" className="w-fit" disabled={pending}>
        {pending ? "Saving…" : hasPassword ? "Change password" : "Set password"}
      </Button>
    </form>
  );
}

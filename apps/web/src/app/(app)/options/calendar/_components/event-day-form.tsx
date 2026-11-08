"use client";

import { useActionState, useId } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { OptionsFormResult } from "@/lib/options/write";

type AddAction = (
  previous: OptionsFormResult | null,
  formData: FormData,
) => Promise<OptionsFormResult>;

/** Add a day no sleeve trades. The API refuses a date that is already one, and says so here. */
export function EventDayForm({ action }: { action: AddAction }) {
  const [result, formAction, pending] = useActionState(action, null);
  const dateId = useId();
  const noteId = useId();
  return (
    <form
      action={formAction}
      className="flex flex-wrap items-end gap-3"
      data-testid="options-event-day-form"
    >
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={dateId}>Day</Label>
        <Input id={dateId} name="date" type="date" required />
      </div>
      <div className="flex min-w-[14rem] flex-1 flex-col gap-1.5">
        <Label htmlFor={noteId}>Why (optional)</Label>
        <Input
          id={noteId}
          name="note"
          maxLength={280}
          placeholder="e.g. state election results"
        />
      </div>
      <Button type="submit" disabled={pending}>
        {pending ? "Adding…" : "Add event day"}
      </Button>
      {result ? (
        <p
          role="status"
          className={
            result.ok
              ? "w-full text-sm text-positive"
              : "w-full text-sm text-negative"
          }
        >
          {result.ok ? result.message : result.error}
        </p>
      ) : null}
    </form>
  );
}

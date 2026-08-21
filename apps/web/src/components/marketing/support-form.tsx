"use client";

import { useActionState, useId } from "react";

import { sendSupportMessage } from "@/app/actions/support";
import { FormStatus } from "@/components/auth/form-status";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SUPPORT_TOPICS } from "@/lib/marketing/support-topics";

/**
 * The contact form — Prompt 18 §2.
 *
 * A native `<select>` rather than the app's Radix select: this page is statically generated and
 * measured for Lighthouse performance, and a native select is the one control that needs no
 * JavaScript to be accessible on every platform including a phone.
 *
 * On success the form is replaced by the confirmation rather than cleared, because a cleared form
 * beside a green message reads as "send it again".
 */
export function SupportForm() {
  const [result, action, pending] = useActionState(sendSupportMessage, null);
  const nameId = useId();
  const emailId = useId();
  const topicId = useId();
  const messageId = useId();

  if (result?.ok) {
    return (
      <div className="rounded-lg border border-border bg-muted/40 p-5">
        <h2 className="text-sm font-medium">Message sent</h2>
        <p className="mt-2 max-w-prose text-sm text-muted-foreground">{result.message}</p>
      </div>
    );
  }

  return (
    <form action={action} className="flex max-w-xl flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-2">
          <Label htmlFor={nameId}>Your name</Label>
          <Input id={nameId} name="name" autoComplete="name" required maxLength={120} />
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor={emailId}>Email</Label>
          <Input
            id={emailId}
            name="email"
            type="email"
            autoComplete="email"
            required
            maxLength={254}
          />
        </div>
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor={topicId}>Subject</Label>
        <select
          id={topicId}
          name="topic"
          required
          defaultValue={SUPPORT_TOPICS[0]}
          className="h-9 rounded-md border border-input bg-card px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {SUPPORT_TOPICS.map((topic) => (
            <option key={topic} value={topic}>
              {topic}
            </option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor={messageId}>Message</Label>
        <textarea
          id={messageId}
          name="message"
          required
          minLength={20}
          maxLength={4000}
          rows={7}
          className="rounded-md border border-input bg-card px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <p className="text-xs text-muted-foreground">
          If a number looks wrong, the most useful thing you can include is the symbol, the date
          and what you compared it against.
        </p>
      </div>

      <FormStatus result={result} />

      <div>
        <Button type="submit" variant="primary" disabled={pending}>
          {pending ? "Sending…" : "Send message"}
        </Button>
      </div>
    </form>
  );
}

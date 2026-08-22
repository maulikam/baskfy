"use client";

import { useActionState, useId, useState } from "react";

import { sendOtp, signInWithOtp, signInWithPassword, type FormResult } from "@/app/actions/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/**
 * docs/11 §Security: "OTP login as the default path, password optional." The tabs reflect that
 * order rather than the habitual password-first one.
 *
 * Every field has a real `<label>` (docs/08 §"Accessibility & quality bar"), the status line is a
 * live region so a failure is announced rather than only shown, and the submit button reserves its
 * own width while pending so the form does not jump.
 *
 * `next` is the destination the middleware's gate wants the user returned to. It is carried in a
 * hidden field and validated again in the action — a redirect target that arrived in a URL is
 * never trusted on the strength of having arrived in a URL.
 *
 * ## The two forms, and why you only see one field (M36)
 *
 * Requesting a code and redeeming it are two server actions, so they are two `<form>` elements —
 * a form posts to one action. Until M36 that leaked into the interface: **both were on screen at
 * once, each with its own "Email" input**, so the first thing the default sign-in path asked you
 * to do was type your address twice into two identical boxes stacked on top of each other.
 *
 * The email is now held in React state. The second form carries it in a hidden field and states
 * it back — "We sent a code to ..." — and only appears once a code has actually been sent. Two
 * forms still, one question.
 */
type Mode = "otp" | "password";

function Status({ result }: { result: FormResult | null }) {
  return (
    <p
      role="status"
      aria-live="polite"
      className={cn(
        "min-h-5 text-sm",
        result?.ok === false ? "text-negative" : "text-muted-foreground",
      )}
    >
      {result?.message ?? ""}
    </p>
  );
}

export function LoginForm({ next }: { next?: string | undefined }) {
  const [mode, setMode] = useState<Mode>("otp");
  /**
   * What was typed into the request form, so the redeem form does not have to ask again. Held
   * here rather than read back off the DOM because the two forms are siblings, not nested.
   */
  const [email, setEmail] = useState("");
  const emailId = useId();
  const codeId = useId();
  const passwordId = useId();

  const [otpRequest, requestAction, requesting] = useActionState(sendOtp, null);
  const [otpResult, otpAction, verifying] = useActionState(signInWithOtp, null);
  const [passwordResult, passwordAction, signingIn] = useActionState(signInWithPassword, null);

  /** A code is on its way, so the box to type it into is worth showing. */
  const sent = otpRequest?.ok === true;

  return (
    <div className="space-y-4">
      <div role="tablist" aria-label="Sign-in method" className="flex gap-1 rounded-md bg-muted p-1">
        {(["otp", "password"] as const).map((value) => (
          <button
            key={value}
            role="tab"
            type="button"
            aria-selected={mode === value}
            onClick={() => setMode(value)}
            className={cn(
              "flex-1 rounded-sm px-3 py-1.5 text-sm transition-colors duration-100",
              mode === value ? "bg-card font-medium shadow-sm" : "text-muted-foreground",
            )}
          >
            {value === "otp" ? "One-time code" : "Password"}
          </button>
        ))}
      </div>

      {mode === "otp" ? (
        <div className="space-y-4">
          <form action={requestAction} className="space-y-2">
            <Label htmlFor={emailId}>Email</Label>
            <Input
              id={emailId}
              name="email"
              type="email"
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
            <Button type="submit" variant="outline" className="w-full" disabled={requesting}>
              {requesting ? "Sending…" : sent ? "Send another code" : "Send me a code"}
            </Button>
          </form>
          <Status result={otpRequest} />

          {/*
            Only after a code has gone out. Showing the code box first asks for something the
            reader does not have yet, which is the same defect as asking for the email twice.
          */}
          {sent ? (
            <form action={otpAction} className="space-y-2">
              {next ? <input type="hidden" name="next" value={next} /> : null}
              {/* Not a second visible field: the address is stated above and travels hidden. */}
              <input type="hidden" name="email" value={email} />
              <Label htmlFor={codeId}>Six-digit code</Label>
              <Input
                id={codeId}
                name="code"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6}"
                className="tnum text-center text-lg tracking-[0.4em]"
                required
              />
              <Button type="submit" variant="primary" className="w-full" disabled={verifying}>
                {verifying ? "Checking…" : "Sign in"}
              </Button>
            </form>
          ) : null}
          <Status result={otpResult} />
        </div>
      ) : (
        <div className="space-y-4">
          <form action={passwordAction} className="space-y-2">
            {next ? <input type="hidden" name="next" value={next} /> : null}
            <Label htmlFor={`${emailId}-pw`}>Email</Label>
            <Input id={`${emailId}-pw`} name="email" type="email" autoComplete="email" required />
            <Label htmlFor={passwordId}>Password</Label>
            <Input
              id={passwordId}
              name="password"
              type="password"
              autoComplete="current-password"
              required
            />
            <Button type="submit" variant="primary" className="w-full" disabled={signingIn}>
              {signingIn ? "Signing in…" : "Sign in"}
            </Button>
          </form>
          <Status result={passwordResult} />
        </div>
      )}
    </div>
  );
}

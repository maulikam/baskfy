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
  const emailId = useId();
  const codeId = useId();
  const passwordId = useId();

  const [otpRequest, requestAction, requesting] = useActionState(sendOtp, null);
  const [otpResult, otpAction, verifying] = useActionState(signInWithOtp, null);
  const [passwordResult, passwordAction, signingIn] = useActionState(signInWithPassword, null);

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
            <Input id={emailId} name="email" type="email" autoComplete="email" required />
            <Button type="submit" variant="outline" className="w-full" disabled={requesting}>
              {requesting ? "Sending…" : "Send me a code"}
            </Button>
          </form>
          <Status result={otpRequest} />

          <form action={otpAction} className="space-y-2">
            {next ? <input type="hidden" name="next" value={next} /> : null}
            <Label htmlFor={`${emailId}-otp`}>Email</Label>
            <Input id={`${emailId}-otp`} name="email" type="email" autoComplete="email" required />
            <Label htmlFor={codeId}>Six-digit code</Label>
            <Input
              id={codeId}
              name="code"
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="[0-9]{6}"
              className="tnum"
              required
            />
            <Button type="submit" variant="primary" className="w-full" disabled={verifying}>
              {verifying ? "Checking…" : "Sign in"}
            </Button>
          </form>
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

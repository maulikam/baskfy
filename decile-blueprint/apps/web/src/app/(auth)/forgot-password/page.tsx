import type { Metadata } from "next";
import Link from "next/link";

import { ForgotPasswordForm } from "@/app/(auth)/forgot-password/forgot-form";

export const metadata: Metadata = {
  title: "Forgot password",
  robots: { index: false, follow: false },
};

/**
 * `POST /auth/forgot-password` — docs/07 §"Account & billing".
 *
 * The response wording deliberately does not confirm whether the address exists — docs/11
 * §Security treats account enumeration as a real risk, and a reset form that says "no such user"
 * is the most common way to hand an attacker a user list. The sentence shown is the API's own, so
 * the two cannot drift into saying different things.
 */
export default function ForgotPasswordPage() {
  return (
    <main className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">Forgot password</h1>
        <p className="text-sm text-muted-foreground">
          Enter your email and we will send a reset link. A one-time code works too, and needs no
          password at all.
        </p>
      </div>

      <ForgotPasswordForm />

      <p className="text-sm text-muted-foreground">
        <Link href="/login" className="text-accent underline-offset-4 hover:underline">
          Back to sign in
        </Link>
      </p>
    </main>
  );
}

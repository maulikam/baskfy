import type { Metadata } from "next";
import Link from "next/link";

import { RegisterForm } from "@/app/(auth)/register/register-form";

export const metadata: Metadata = {
  title: "Create an account",
  robots: { index: false, follow: false },
};

/**
 * `POST /auth/register` — docs/07 §"Account & billing", Prompt 12 deliverable 4.
 *
 * The password field is optional, because docs/11 §Security makes OTP "the default path, password
 * optional". The consent checkbox is not optional: docs/11 §Compliance requires a DPDP consent
 * record, and the API refuses a registration without one.
 */
export default function RegisterPage() {
  return (
    <main className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">Create an account</h1>
        <p className="text-sm text-muted-foreground">
          A password is optional — you can sign in with a one-time code instead.
        </p>
      </div>

      <RegisterForm />

      <p className="text-sm text-muted-foreground">
        Already have an account?{" "}
        <Link href="/login" className="text-accent underline-offset-4 hover:underline">
          Sign in
        </Link>
      </p>
    </main>
  );
}

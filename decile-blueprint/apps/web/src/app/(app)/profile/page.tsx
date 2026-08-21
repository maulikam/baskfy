import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { DangerZone } from "@/components/account/danger-zone";
import { ProfileForm } from "@/components/account/profile-form";
import { Badge } from "@/components/ui/badge";
import { formatTradeDate } from "@/lib/format";
import { fetchMe } from "@/lib/auth/me";

export const metadata: Metadata = {
  title: "Profile",
  robots: { index: false, follow: false },
};

/**
 * `/profile` — docs/08 §Routes: "RSC + forms". Prompt 12 deliverable 4.
 *
 * Reads `GET /me`, which docs/07 defines as "profile + entitlements", so the page shows what the
 * account *is* and what it may *do* in one place. The entitlements are read-only here: docs/07
 * §Entitlements is explicit that "the UI only *reflects* entitlements".
 */
export default async function ProfilePage() {
  const me = await fetchMe();
  if (!me) redirect("/login?next=/profile");

  return (
    <div className="flex max-w-2xl flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight">Profile</h1>
        <p className="text-sm text-muted-foreground">
          Signed in as {me.email}. Member since {formatTradeDate(me.created_at.slice(0, 10))}.
        </p>
      </header>

      {me.deletion_scheduled_for ? (
        <p className="rounded-md border border-warning/30 bg-warning-muted px-3 py-2 text-sm">
          This account is scheduled for deletion on{" "}
          {formatTradeDate(me.deletion_scheduled_for.slice(0, 10))}. Signing in again before then
          cancels it.
        </p>
      ) : null}

      <section aria-labelledby="details-heading" className="flex flex-col gap-3">
        <h2 id="details-heading" className="text-sm font-semibold">
          Details
        </h2>
        <ProfileForm name={me.name ?? ""} email={me.email} verified={me.email_verified} />
      </section>

      <section aria-labelledby="security-heading" className="flex flex-col gap-3">
        <h2 id="security-heading" className="text-sm font-semibold">
          Security
        </h2>
        <p className="text-sm text-muted-foreground">
          {me.has_password
            ? "This account has a password."
            : "This account signs in with one-time codes only."}{" "}
          <Link
            href="/change-password"
            className="text-accent underline-offset-4 hover:underline"
          >
            {me.has_password ? "Change password" : "Set a password"}
          </Link>
        </p>
      </section>

      <section aria-labelledby="plan-heading" className="flex flex-col gap-3">
        <h2 id="plan-heading" className="text-sm font-semibold">
          Plan
        </h2>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={me.subscription_status === "active" ? "positive" : "neutral"}>
            {me.plan_code ?? "No plan"}
          </Badge>
          {me.subscription_status ? (
            <span className="text-sm text-muted-foreground">{me.subscription_status}</span>
          ) : null}
        </div>
        <ul aria-label="Entitlements" className="flex flex-wrap gap-1.5">
          {(
            [
              ["screener", "Screener"],
              ["export_csv", "CSV export"],
              ["custom_columns", "Custom columns"],
              ["historical_ranks", "Historical ranks"],
              ["backtests", "Backtests"],
              ["api_access", "API access"],
            ] as const
          ).map(([key, label]) => (
            <li key={key}>
              <Badge variant={me.entitlements[key] ? "positive" : "neutral"}>
                {label}
                <span className="sr-only">
                  {me.entitlements[key] ? " included" : " not included"}
                </span>
              </Badge>
            </li>
          ))}
        </ul>
      </section>

      <DangerZone email={me.email} />
    </div>
  );
}

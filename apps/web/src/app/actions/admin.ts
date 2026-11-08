"use server";

import { revalidatePath } from "next/cache";
import type { ResyncPlanOut } from "@baskfy/api-client";

import { serverApi } from "@/lib/api/server";

/**
 * The staff surface's mutations — PROMPTS.md Prompt 17 deliverable 4.
 *
 * Server actions rather than client fetches for one reason: **the bearer token never leaves the
 * server.** `serverApi()` reads it from the Auth.js session and attaches it; a client component
 * doing the same would need the token in the browser, and a staff token is the one token that
 * should not be there.
 *
 * Every action returns `{ ok, message }` rather than throwing. A thrown server action renders the
 * error boundary and loses the page the operator was reading, which at 3am is exactly the wrong
 * behaviour — the message belongs beside the button that produced it.
 *
 * None of these decides anything. `require_staff` on the API refuses a non-staff caller with a
 * 404 (`docs/DECISIONS.md` §17.3), and every one of them writes an `admin_action` row naming the
 * caller. This layer is a form post.
 */

export interface AdminActionResult {
  ok: boolean;
  message: string;
}

/** RFC 9457 bodies carry `detail`; anything else is unexpected and says so. */
function problemDetail(error: unknown, fallback: string): string {
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail) return detail;
  }
  return fallback;
}

/** docs/09 §Observability's operator UI, the "re-run button" half. */
export async function rerunPipeline(tradeDate: string): Promise<AdminActionResult> {
  const api = await serverApi();
  const { data, error } = await api.POST("/api/v1/admin/pipeline/runs/{trade_date}/rerun", {
    params: { path: { trade_date: tradeDate } },
  });
  if (!data) {
    return { ok: false, message: problemDetail(error, "The re-run could not be queued.") };
  }
  revalidatePath("/admin/pipeline");
  return {
    ok: true,
    message: `Queued ${data.task} for ${data.target} (task ${data.task_id}). Steps appear here as they run.`,
  };
}

/** docs/09 §"Adjustment algorithm"'s rebuild rule, as a button. Idempotent by construction. */
export async function reprocessInstrument(symbol: string): Promise<AdminActionResult> {
  const trimmed = symbol.trim().toUpperCase();
  if (!trimmed) return { ok: false, message: "Enter a symbol." };

  const api = await serverApi();
  const { data, error } = await api.POST("/api/v1/admin/instruments/{symbol}/reprocess", {
    params: { path: { symbol: trimmed } },
  });
  if (!data) {
    return { ok: false, message: problemDetail(error, `${trimmed} could not be reprocessed.`) };
  }
  revalidatePath("/admin");
  return {
    ok: true,
    message: `Queued a rebuild of ${data.target}'s adjusted history (task ${data.task_id}).`,
  };
}

export interface OverrideInput {
  publicId: string;
  feature: string;
  effect: "grant" | "revoke";
  reason: string;
  value?: number | null;
  expiresAt?: string | null;
}

/**
 * Grant or revoke one feature for one account.
 *
 * The expiry is not enforced here — the API accepts a NULL one, because a genuine comp account is
 * a real case. The *form* defaults to a date, because a support grant with no end is how a comp
 * account is created by accident.
 */
export async function setEntitlementOverride(input: OverrideInput): Promise<AdminActionResult> {
  const reason = input.reason.trim();
  if (reason.length < 3) {
    return { ok: false, message: "Give a reason. It is stored with the grant and with your name." };
  }

  const api = await serverApi();
  const { data, error } = await api.PUT("/api/v1/admin/users/{public_id}/entitlements", {
    params: { path: { public_id: input.publicId } },
    body: {
      feature: input.feature,
      effect: input.effect,
      reason,
      value: input.value ?? null,
      expires_at: input.expiresAt ? new Date(input.expiresAt).toISOString() : null,
    },
  });
  if (!data) {
    return { ok: false, message: problemDetail(error, "The override could not be saved.") };
  }
  revalidatePath("/admin/users");
  const verb = input.effect === "grant" ? "Granted" : "Revoked";
  return { ok: true, message: `${verb} ${input.feature} for ${data.user.email}.` };
}

export async function clearEntitlementOverride(
  publicId: string,
  feature: string,
): Promise<AdminActionResult> {
  const api = await serverApi();
  const { error, response } = await api.DELETE(
    "/api/v1/admin/users/{public_id}/entitlements/{feature}",
    { params: { path: { public_id: publicId, feature } } },
  );
  if (!response.ok) {
    return { ok: false, message: problemDetail(error, "The override could not be removed.") };
  }
  revalidatePath("/admin/users");
  return { ok: true, message: `Removed the ${feature} override. The plan decides again.` };
}

/**
 * Leaf 3.1's resync button, both halves.
 *
 * Two actions rather than one with a flag, because inspect-then-act is the shape of the feature:
 * the operator is on a phone and reads what is about to happen before it happens, and a flag that
 * turns a read into a write is one typo away from a repair nobody asked for.
 */

export interface ResyncInspection extends AdminActionResult {
  plan: ResyncPlanOut | null;
}

/** The dry inspection. Changes nothing — safe to poll. */
export async function inspectResync(): Promise<ResyncInspection> {
  const api = await serverApi();
  const { data, error } = await api.GET("/api/v1/admin/resync");
  if (!data) {
    return {
      ok: false,
      plan: null,
      message: problemDetail(error, "The data check could not run."),
    };
  }
  return { ok: true, plan: data, message: "" };
}

/**
 * Close every gap the inspection found.
 *
 * 202, so this returns as soon as the work is queued. What it managed lands in
 * `plan.last_resync`, which is why the panel polls the inspection afterwards rather than
 * declaring success here — a repair that fixed three of five gaps must not be reported as done.
 */
export async function startResync(): Promise<AdminActionResult> {
  const api = await serverApi();
  const { data, error } = await api.POST("/api/v1/admin/resync");
  if (!data) {
    return { ok: false, message: problemDetail(error, "The resync could not be started.") };
  }
  revalidatePath("/admin");
  return { ok: true, message: data.detail };
}

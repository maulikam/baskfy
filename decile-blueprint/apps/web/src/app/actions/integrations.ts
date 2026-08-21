"use server";

import { revalidatePath } from "next/cache";

import { serverApi } from "@/lib/api/server";

/**
 * API keys and screen alerts, as server actions — PROMPTS.md Prompt 20 §1 and §3.
 *
 * Server actions for the same reason `admin.ts` uses them: **the bearer token never leaves the
 * server.** That matters more here than anywhere else in the app, because the response to
 * `POST /keys` carries a credential — the one and only time its plaintext exists. Doing this from
 * a client component would put both the session token and the new key through the browser's fetch
 * stack and into whatever an extension can see.
 *
 * Every action returns a result object rather than throwing: a thrown server action renders the
 * error boundary and loses the page, and "your key was created but we lost it" is not a message
 * anyone can act on.
 */

export interface ActionResult {
  ok: boolean;
  message: string;
}

/** The one result that carries a secret. It is returned to the caller and never stored. */
export interface CreatedKeyResult extends ActionResult {
  /** `null` on failure. Shown once, in a dialog the user must dismiss deliberately. */
  secret: string | null;
  display: string | null;
}

/** RFC 9457 bodies carry `detail`; anything else is unexpected and says so. */
function problemDetail(error: unknown, fallback: string): string {
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail) return detail;
  }
  return fallback;
}

export async function createApiKey(name: string): Promise<CreatedKeyResult> {
  const trimmed = name.trim();
  if (!trimmed) return { ok: false, message: "Give the key a name.", secret: null, display: null };

  const api = await serverApi();
  const { data, error } = await api.POST("/api/v1/keys", { body: { name: trimmed } });
  if (!data) {
    return {
      ok: false,
      message: problemDetail(error, "The key could not be created."),
      secret: null,
      display: null,
    };
  }
  revalidatePath("/api-keys");
  return {
    ok: true,
    message: "Copy this key now. It is not stored and cannot be shown again.",
    secret: data.secret,
    display: data.key.display,
  };
}

export async function rotateApiKey(publicId: string): Promise<CreatedKeyResult> {
  const api = await serverApi();
  const { data, error } = await api.POST("/api/v1/keys/{public_id}/rotate", {
    params: { path: { public_id: publicId } },
  });
  if (!data) {
    return {
      ok: false,
      message: problemDetail(error, "The key could not be rotated."),
      secret: null,
      display: null,
    };
  }
  revalidatePath("/api-keys");
  return {
    ok: true,
    message: "The old key stopped working immediately. Copy the new one now.",
    secret: data.secret,
    display: data.key.display,
  };
}

export async function revokeApiKey(publicId: string, reason: string): Promise<ActionResult> {
  const api = await serverApi();
  const { data, error } = await api.POST("/api/v1/keys/{public_id}/revoke", {
    params: { path: { public_id: publicId } },
    body: { reason: reason.trim() || "revoked" },
  });
  if (!data) return { ok: false, message: problemDetail(error, "The key could not be revoked.") };
  revalidatePath("/api-keys");
  return { ok: true, message: `${data.display} is revoked. The next request with it is refused.` };
}

export async function subscribeScreen(
  screenPublicId: string,
  frequency: "daily" | "weekly",
  digest: boolean,
): Promise<ActionResult> {
  if (!screenPublicId) return { ok: false, message: "Pick a screen." };
  const api = await serverApi();
  const { data, error } = await api.POST("/api/v1/alerts", {
    body: { screen_public_id: screenPublicId, frequency, digest },
  });
  if (!data) {
    return { ok: false, message: problemDetail(error, "The alert could not be created.") };
  }
  revalidatePath("/alerts");
  return {
    ok: true,
    message: `Alerts on for “${data.screen_name}”. The first one arrives after the next publish.`,
  };
}

export async function setAlertActive(publicId: string, isActive: boolean): Promise<ActionResult> {
  const api = await serverApi();
  const { data, error } = await api.PATCH("/api/v1/alerts/{public_id}", {
    params: { path: { public_id: publicId } },
    body: { is_active: isActive },
  });
  if (!data) return { ok: false, message: problemDetail(error, "The alert could not be updated.") };
  revalidatePath("/alerts");
  return { ok: true, message: data.is_active ? "Alert on." : "Alert paused." };
}

export async function setAlertDigest(publicId: string, digest: boolean): Promise<ActionResult> {
  const api = await serverApi();
  const { data, error } = await api.PATCH("/api/v1/alerts/{public_id}", {
    params: { path: { public_id: publicId } },
    body: { digest },
  });
  if (!data) return { ok: false, message: problemDetail(error, "The alert could not be updated.") };
  revalidatePath("/alerts");
  return {
    ok: true,
    message: data.digest
      ? "This screen now travels in the combined digest."
      : "This screen now gets its own email.",
  };
}

export async function deleteAlert(publicId: string): Promise<ActionResult> {
  const api = await serverApi();
  const { error, response } = await api.DELETE("/api/v1/alerts/{public_id}", {
    params: { path: { public_id: publicId } },
  });
  if (response.status !== 204) {
    return { ok: false, message: problemDetail(error, "The alert could not be deleted.") };
  }
  revalidatePath("/alerts");
  return { ok: true, message: "Alert deleted." };
}

/**
 * The one-click unsubscribe target. **Unauthenticated on purpose** — see
 * `decile_api.routers.alerts`: the link is followed by a mail client, a link scanner, or a person
 * who is not signed in, and requiring a session would make it useless to all three.
 */
export async function unsubscribeFromAlert(token: string): Promise<ActionResult> {
  const api = await serverApi();
  const { data, error } = await api.POST("/api/v1/alerts/unsubscribe", { body: { token } });
  if (!data) {
    return { ok: false, message: problemDetail(error, "That link could not be processed.") };
  }
  return {
    ok: true,
    message: data.screen_name
      ? `You will not get any more alerts about “${data.screen_name}”.`
      : "You will not get any more alerts from that link.",
  };
}

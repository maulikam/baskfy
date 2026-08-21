"use server";

import { apiOrigin } from "@/lib/api/config";
import type { MutationResult } from "@/lib/auth/me";
import { SUPPORT_TOPICS } from "@/lib/marketing/support-topics";

/**
 * The `/support` contact form's action — Prompt 18 §2.
 *
 * A server action rather than a browser `fetch`, for one reason that matters: the CSP in
 * `src/middleware.ts` names the API origin in `connect-src`, and a *statically generated* page
 * gets the nonce-free policy, so a browser POST from the marketing group would be a second
 * cross-origin surface to keep in step. A server action posts same-origin and the server makes
 * the API call, which also means the visitor's address never appears in a request the browser's
 * own extensions can read.
 *
 * `MutationResult` is the shape every other form in the app already reports through, so
 * `FormStatus` renders this one too.
 */

const UNREACHABLE = "We could not reach the support service. Try again in a moment, please.";

/** Mirrors the API's own bound (`SupportMessageIn.message`, min 20). Checked again server-side. */
const MIN_MESSAGE_LENGTH = 20;

function field(formData: FormData, name: string): string {
  const value = formData.get(name);
  return typeof value === "string" ? value.trim() : "";
}

function isTopic(value: string): boolean {
  return (SUPPORT_TOPICS as readonly string[]).includes(value);
}

interface ProblemBody {
  detail?: unknown;
}

export async function sendSupportMessage(
  _previous: MutationResult | null,
  formData: FormData,
): Promise<MutationResult> {
  const name = field(formData, "name");
  const email = field(formData, "email");
  const topic = field(formData, "topic");
  const message = field(formData, "message");

  /* Client-side-equivalent checks, run on the server. They exist to give a specific message
     rather than the API's generic validation problem — not to be the enforcement, which is
     `SupportMessageIn`. */
  if (!name) return { ok: false, message: "Tell us who you are." };
  if (!email.includes("@")) return { ok: false, message: "That does not look like an email address." };
  if (!isTopic(topic)) return { ok: false, message: "Pick a subject." };
  if (message.length < MIN_MESSAGE_LENGTH) {
    return { ok: false, message: "Please say a little more — at least a sentence or two." };
  }

  let response: Response;
  try {
    response = await fetch(`${apiOrigin()}/api/v1/support`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, email, topic, message }),
      cache: "no-store",
    });
  } catch {
    return { ok: false, message: UNREACHABLE };
  }

  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ProblemBody;
    const detail = typeof body.detail === "string" ? body.detail : UNREACHABLE;
    return { ok: false, message: detail };
  }

  return {
    ok: true,
    message: "Thanks — your message has been sent, and a copy is on its way to your inbox.",
  };
}

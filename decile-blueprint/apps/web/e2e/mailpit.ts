import type { APIRequestContext } from "@playwright/test";

/**
 * Reading the inbox — Prompt 12 §3: "local delivery to **mailpit**".
 *
 * Mailpit accepts SMTP on 1025 and serves what it caught over HTTP on 8025. The browser suite
 * reads the sign-in code and the confirmation link out of it exactly as a developer reads them in
 * mailpit's own UI, which is what makes the register → verify → login flow testable end to end
 * without stubbing the one part most likely to be wrong.
 */
const MAILPIT_URL = process.env.DECILE_MAILPIT_URL ?? "http://127.0.0.1:8025";

interface Summary {
  ID: string;
  Subject: string;
  To: { Address: string }[];
  Created: string;
}

interface Detail {
  ID: string;
  Subject: string;
  Text: string;
  HTML: string;
}

/** Empty the inbox, so a test reads its own mail and not the previous test's. */
export async function clearInbox(request: APIRequestContext): Promise<void> {
  await request.delete(`${MAILPIT_URL}/api/v1/messages`);
}

async function search(request: APIRequestContext, address: string): Promise<Summary[]> {
  const response = await request.get(
    `${MAILPIT_URL}/api/v1/search?query=${encodeURIComponent(`to:${address}`)}&limit=20`,
  );
  if (!response.ok()) return [];
  const payload = (await response.json()) as { messages?: Summary[] };
  return payload.messages ?? [];
}

/**
 * The newest message for an address, polled until it lands.
 *
 * Delivery is asynchronous — the endpoint answers before the SMTP conversation completes — so a
 * single read would be flaky by construction.
 */
export async function waitForEmail(
  request: APIRequestContext,
  address: string,
  options: { subjectContains?: string; timeoutMs?: number } = {},
): Promise<Detail> {
  const deadline = Date.now() + (options.timeoutMs ?? 15_000);
  let seen: string[] = [];

  while (Date.now() < deadline) {
    const messages = await search(request, address);
    seen = messages.map((message) => message.Subject);
    const match = options.subjectContains
      ? messages.find((message) => message.Subject.includes(options.subjectContains ?? ""))
      : messages[0];
    if (match) {
      const detail = await request.get(`${MAILPIT_URL}/api/v1/message/${match.ID}`);
      return (await detail.json()) as Detail;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(
    `no email for ${address}${options.subjectContains ? ` matching "${options.subjectContains}"` : ""}; saw: ${seen.join(", ") || "nothing"}`,
  );
}

/** The six-digit sign-in code out of the plain-text body. */
export function codeFrom(message: Detail): string {
  const match = /^\s*(\d{6})\s*$/m.exec(message.Text);
  if (!match?.[1]) throw new Error(`no six-digit code in:\n${message.Text}`);
  return match[1];
}

/** The `?token=…` value out of the first link in the plain-text body. */
export function tokenFrom(message: Detail): string {
  const match = /token=([A-Za-z0-9._~-]+)/.exec(message.Text);
  if (!match?.[1]) throw new Error(`no token link in:\n${message.Text}`);
  return match[1];
}

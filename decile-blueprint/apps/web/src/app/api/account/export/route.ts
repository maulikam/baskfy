import { NextResponse } from "next/server";

import { serverApiOrigin } from "@/lib/api/config";
import { auth } from "@/lib/auth";

/**
 * A download for `GET /me/export` — docs/11 §Compliance's DPDP data export.
 *
 * A route handler rather than a link straight at the API, because the API authenticates with a
 * bearer header and a browser navigation cannot send one. This attaches the session's token
 * server-side and streams the JSON back as an attachment, so "download my data" is one click.
 */
export const runtime = "nodejs";

export async function GET(): Promise<NextResponse> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token) return NextResponse.json({ error: "not signed in" }, { status: 401 });

  const response = await fetch(`${serverApiOrigin()}/api/v1/me/export`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!response.ok) {
    return NextResponse.json({ error: "export failed" }, { status: response.status });
  }

  const body = await response.text();
  return new NextResponse(body, {
    headers: {
      "content-type": "application/json",
      "content-disposition": 'attachment; filename="baskfy-account-export.json"',
      // A personal-data export must never sit in a shared cache.
      "cache-control": "no-store, private",
    },
  });
}

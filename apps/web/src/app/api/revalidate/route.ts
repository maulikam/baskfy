import { revalidateTag } from "next/cache";
import { NextResponse } from "next/server";

import { FACTSHEET_TAG } from "@/lib/instrument/fetch";

/**
 * The hook that makes the instrument pages' ISR "revalidated on `data_version`" (Prompt 10
 * deliverable 6) true rather than merely time-based.
 *
 * docs/09 §Schedule ends the nightly chain with the publish step, which bumps `data_version`.
 * That step calls this route; every factsheet carries `FACTSHEET_TAG`, so one call invalidates
 * all of them and the next request for each renders the new day.
 *
 * Guarded by a shared secret compared in constant time. Without `REVALIDATE_SECRET` the route
 * refuses every request rather than defaulting to open: an unauthenticated cache-purge endpoint
 * is a free way to make the site re-render on demand.
 */
export const runtime = "nodejs";

function constantTimeEquals(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let index = 0; index < a.length; index += 1) {
    difference |= a.charCodeAt(index) ^ b.charCodeAt(index);
  }
  return difference === 0;
}

export function POST(request: Request): NextResponse {
  const expected = process.env.REVALIDATE_SECRET;
  if (!expected) {
    return NextResponse.json(
      { revalidated: false, reason: "REVALIDATE_SECRET is not configured" },
      { status: 503 },
    );
  }

  const supplied = request.headers.get("x-revalidate-secret") ?? "";
  if (!constantTimeEquals(supplied, expected)) {
    return NextResponse.json({ revalidated: false }, { status: 401 });
  }

  revalidateTag(FACTSHEET_TAG);
  return NextResponse.json({ revalidated: true, tag: FACTSHEET_TAG });
}

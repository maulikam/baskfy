import { POST_META_BY_DATE } from "@/lib/marketing/post-meta";
import { renderFeed } from "@/lib/marketing/rss";

/**
 * `/blog/rss.xml` — Prompt 18 §2.
 *
 * The document itself is built by `lib/marketing/rss`, which is where it can be unit-tested: a
 * Next route module may only export the HTTP verbs and a fixed set of config keys, so a helper
 * exported from here is a build error.
 *
 * `force-static` because the feed is exactly as static as the posts are — it is generated at build
 * time and written to disk, which is what keeps it inside Prompt 18's first acceptance criterion.
 * It changes when a post is added, which is a deploy.
 */
export const dynamic = "force-static";

export function GET(): Response {
  return new Response(renderFeed(POST_META_BY_DATE), {
    headers: {
      "content-type": "application/rss+xml; charset=utf-8",
      "cache-control": "public, max-age=3600, stale-while-revalidate=86400",
    },
  });
}

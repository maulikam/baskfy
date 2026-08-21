import type { Metadata } from "next";
import Link from "next/link";

import { POST_META_BY_DATE } from "@/lib/marketing/post-meta";
import { formatTradeDate } from "@/lib/format";
import { SITE_NAME } from "@/lib/site";

/**
 * `/blog` — docs/01 §1 (Content), docs/08 §Routes ("SSG").
 *
 * The feed is declared with a `<link rel="alternate">` in `metadata.alternates.types`, which is
 * what a reader's autodiscovery looks for, and linked visibly as well — a feed nobody can find is
 * a file on a disk.
 */
export const metadata: Metadata = {
  title: "Blog",
  description: `How ${SITE_NAME} computes what it computes, and what it has not solved yet.`,
  alternates: {
    canonical: "/blog",
    types: { "application/rss+xml": [{ url: "/blog/rss.xml", title: `${SITE_NAME} — blog` }] },
  },
};

export default function BlogIndexPage() {
  return (
    <div className="mx-auto max-w-3xl px-6 py-14">
      <h1 className="text-2xl font-semibold tracking-tight">Blog</h1>
      <p className="mt-3 max-w-prose text-muted-foreground">
        Method, not marketing. Each post is about a decision in the build and the reasoning behind
        it, including the ones that did not go well.{" "}
        <a className="underline underline-offset-2" href="/blog/rss.xml">
          RSS
        </a>
        .
      </p>

      <ul className="mt-10 divide-y divide-border border-y border-border">
        {POST_META_BY_DATE.map((post) => (
          <li key={post.slug} className="py-6">
            <article>
              <h2 className="font-medium">
                <Link className="hover:underline" href={`/blog/${post.slug}`}>
                  {post.title}
                </Link>
              </h2>
              <p className="mt-2 max-w-prose text-sm text-muted-foreground">{post.summary}</p>
              <p className="mt-2 text-xs text-muted-foreground">
                <time dateTime={post.date}>{formatTradeDate(post.date)}</time>
              </p>
            </article>
          </li>
        ))}
      </ul>
    </div>
  );
}

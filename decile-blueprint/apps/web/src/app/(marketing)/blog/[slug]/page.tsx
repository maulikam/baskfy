import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { formatTradeDate } from "@/lib/format";
import { POST_META } from "@/lib/marketing/post-meta";
import { findPost } from "@/lib/marketing/posts";
import { SITE_NAME, SITE_URL } from "@/lib/site";

/**
 * `/blog/[slug]` — docs/08 §Routes marks `/blog/*` SSG, and `generateStaticParams` is what makes
 * it so: every post is prerendered at build time and `dynamicParams = false` turns an unknown
 * slug into a 404 rather than into an on-demand render of a post that does not exist.
 *
 * `BlogPosting` JSON-LD (Prompt 18 §4) is emitted per post, with the canonical URL as its `@id`.
 */
export const dynamicParams = false;

export function generateStaticParams(): { slug: string }[] {
  return POST_META.map((post) => ({ slug: post.slug }));
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}): Promise<Metadata> {
  const { slug } = await params;
  const post = findPost(slug);
  if (!post) return {};
  return {
    title: post.title,
    description: post.summary,
    alternates: { canonical: `/blog/${post.slug}` },
    openGraph: {
      type: "article",
      title: post.title,
      description: post.summary,
      url: `${SITE_URL}/blog/${post.slug}`,
      publishedTime: post.date,
    },
  };
}

export default async function BlogPostPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const post = findPost(slug);
  if (!post) notFound();

  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "BlogPosting",
    "@id": `${SITE_URL}/blog/${post.slug}`,
    headline: post.title,
    description: post.summary,
    datePublished: post.date,
    inLanguage: "en-IN",
    mainEntityOfPage: `${SITE_URL}/blog/${post.slug}`,
    publisher: { "@type": "Organization", name: SITE_NAME, url: SITE_URL },
  };

  return (
    <article className="mx-auto max-w-3xl px-6 py-14">
      <script
        type="application/ld+json"
        // JSON-LD has no non-`dangerously` form. The payload is `JSON.stringify` of an object
        // built here from the typed post registry — never a raw string, and never user input.
        dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
      />
      <p className="text-xs text-muted-foreground">
        <Link className="hover:underline" href="/blog">
          Blog
        </Link>
        {" · "}
        <time dateTime={post.date}>{formatTradeDate(post.date)}</time>
      </p>
      <h1 className="mt-3 text-2xl font-semibold tracking-tight">{post.title}</h1>
      <div className="mt-8">
        <post.Body />
      </div>
    </article>
  );
}

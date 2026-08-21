import type { ComponentType } from "react";

import PointInTime from "@/content/blog/point-in-time-or-it-did-not-happen.mdx";
import ReproducingAnExport from "@/content/blog/reproducing-a-competitors-export.mdx";
import WhatADecileMeasures from "@/content/blog/what-a-decile-actually-measures.mdx";
import { POST_META, type PostMeta } from "@/lib/marketing/post-meta";

/**
 * The blog index — Prompt 18 §2: "`/blog` with MDX posts and RSS".
 *
 * **This is the only module that imports MDX.** The metadata lives in `post-meta.ts`, which the
 * sitemap, the feed and the tests read; this file joins it to the compiled bodies. See that module
 * for why the split exists.
 *
 * The bodies are imported statically rather than through `await import(slug)`. Three posts is not
 * a scale problem, and a static import means a renamed file is a build error rather than a 404
 * discovered in production. Each body is a **server component** — MDX compiles at build time and
 * ships no client JavaScript.
 */
export type { PostMeta } from "@/lib/marketing/post-meta";

export interface Post extends PostMeta {
  /** Rendered MDX. A server component; it takes no props. */
  Body: ComponentType;
}

const BODIES: Record<string, ComponentType> = {
  "what-a-decile-actually-measures": WhatADecileMeasures,
  "point-in-time-or-it-did-not-happen": PointInTime,
  "reproducing-a-competitors-export": ReproducingAnExport,
};

/**
 * Throws rather than rendering an empty article.
 *
 * A registered post with no body means the registry and this file have gone out of step, which
 * should fail the build. `src/lib/__tests__/marketing-content.test.ts` also asserts that every
 * registered slug has a file on disk, so the two failures bracket the mistake from both sides.
 */
function bodyFor(slug: string): ComponentType {
  const body = BODIES[slug];
  if (!body) throw new Error(`No MDX body imported for post "${slug}"`);
  return body;
}

export const POSTS: readonly Post[] = POST_META.map((meta) => ({
  ...meta,
  Body: bodyFor(meta.slug),
}));

/** Newest first — the order the index uses. */
export const POSTS_BY_DATE: readonly Post[] = [...POSTS].sort((a, b) =>
  b.date.localeCompare(a.date),
);

export function findPost(slug: string): Post | undefined {
  return POSTS.find((post) => post.slug === slug);
}

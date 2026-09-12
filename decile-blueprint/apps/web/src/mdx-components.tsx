import type { MDXComponents } from "mdx/types";
import type { Route } from "next";
import Link from "next/link";

/**
 * How MDX elements render — required by `@next/mdx` at the app root.
 *
 * Tailwind v4 ships no typography plugin here (docs/02 locks Tailwind and shadcn/ui, not
 * `@tailwindcss/typography`), so the prose styles are spelled out once, in this map, rather than
 * as a `prose` class the design tokens would then have to fight.
 *
 * Internal links become `next/link` so a post's cross-reference is a client navigation like every
 * other link in the app; external ones open in a new tab with `rel="noreferrer"`.
 *
 * Each override is typed as taking `children` explicitly rather than spreading an untyped bag:
 * `MDXComponents`' own element props are loose, and `jsx-a11y/heading-has-content` cannot see that
 * a spread carries children. Naming the prop satisfies both the rule and the reader.
 */
type Block = { children?: React.ReactNode };
type Anchor = Block & { href?: string };

export function useMDXComponents(components: MDXComponents): MDXComponents {
  return {
    h1: ({ children }: Block) => (
      <h1 className="text-2xl font-semibold tracking-tight">{children}</h1>
    ),
    h2: ({ children }: Block) => (
      <h2 className="mt-10 text-lg font-semibold tracking-tight">{children}</h2>
    ),
    h3: ({ children }: Block) => <h3 className="mt-8 font-medium">{children}</h3>,
    p: ({ children }: Block) => (
      <p className="mt-4 max-w-prose leading-relaxed text-muted-foreground">{children}</p>
    ),
    ul: ({ children }: Block) => (
      <ul className="mt-4 max-w-prose list-disc space-y-2 pl-5 text-muted-foreground">
        {children}
      </ul>
    ),
    ol: ({ children }: Block) => (
      <ol className="mt-4 max-w-prose list-decimal space-y-2 pl-5 text-muted-foreground">
        {children}
      </ol>
    ),
    li: ({ children }: Block) => <li className="leading-relaxed">{children}</li>,
    strong: ({ children }: Block) => (
      <strong className="font-medium text-foreground">{children}</strong>
    ),
    blockquote: ({ children }: Block) => (
      <blockquote className="mt-4 max-w-prose border-l-2 border-border pl-4 text-muted-foreground">
        {children}
      </blockquote>
    ),
    code: ({ children }: Block) => (
      <code className="rounded bg-muted px-1 py-0.5 text-[0.9em] tabular-nums">{children}</code>
    ),
    pre: ({ children }: Block) => (
      <pre className="mt-4 overflow-x-auto rounded-md border border-border bg-muted/50 p-4 text-xs">
        {children}
      </pre>
    ),
    hr: () => <hr className="my-10 border-border" />,
    table: ({ children }: Block) => (
      <div className="mt-6 overflow-x-auto">
        <table className="w-full border-collapse text-sm tabular-nums">{children}</table>
      </div>
    ),
    th: ({ children }: Block) => (
      <th className="border-b border-border py-2 pr-4 text-left text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {children}
      </th>
    ),
    td: ({ children }: Block) => (
      <td className="border-b border-border/60 py-2 pr-4 align-top">{children}</td>
    ),
    a: ({ href, children }: Anchor) => {
      const target = href ?? "#";
      if (target.startsWith("/")) {
        /* `next/link` is typed against the generated route union; MDX prose is a string, so the
           cast is the one place a link's validity is checked by the build rather than by the type
           system. A broken internal link therefore shows up as a 404, which is why every one of
           them points at a route in `lib/marketing/routes`. */
        return (
          <Link className="underline underline-offset-2" href={target as Route}>
            {children}
          </Link>
        );
      }
      return (
        <a
          className="underline underline-offset-2"
          href={target}
          rel="noreferrer"
          target="_blank"
        >
          {children}
        </a>
      );
    },
    ...components,
  };
}

import Link from "next/link";

/**
 * Marketing-shell 404 (AUDIT 0.10).
 */
export default function MarketingNotFound() {
  return (
    <div className="mx-auto flex max-w-lg flex-col gap-4 px-5 py-16">
      <h1 className="text-2xl font-light tracking-tight">Page not found</h1>
      <p className="text-muted-foreground text-sm leading-relaxed">
        That address is not a page on baskfy.com. Use the links in the header, or go home.
      </p>
      <Link href="/" className="text-sm underline-offset-4 hover:underline">
        Go to the home page
      </Link>
    </div>
  );
}

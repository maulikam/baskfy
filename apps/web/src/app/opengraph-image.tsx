/**
 * Root OG image — re-exported so `/opengraph-image` resolves outside route groups.
 *
 * The generator lives under `(marketing)` because that is where most public pages sit; this file
 * makes the same card available at the canonical App Router path crawlers and metadata expect.
 */
export { alt, contentType, default, size } from "./(marketing)/opengraph-image";

/**
 * Active tab = the longest `href` that is an exact match or a path prefix.
 * A bare `startsWith("/build/")` would light both Screens and Backtests on
 * `/build/backtests` (AUDIT 1.7).
 */
export function sectionTabIsActive(
  pathname: string,
  href: string,
  allHrefs: readonly string[],
): boolean {
  const matches = allHrefs.filter(
    (candidate) => pathname === candidate || pathname.startsWith(`${candidate}/`),
  );
  if (matches.length === 0) return false;
  const best = matches.reduce((a, b) => (a.length >= b.length ? a : b));
  return best === href;
}

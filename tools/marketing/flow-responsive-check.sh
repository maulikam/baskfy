#!/usr/bin/env bash
# Gate G12 for gates/marketing-flow-responsive.md.
#
# The production build, plus the one thing a build's exit code does not tell you: that the
# arbitrary Tailwind utilities this layout is built on were actually *emitted*. A typo'd
# `lg:grid-cols-[minmax(0,3.1fr)_...]` or a `sm:text-[13px]` the scanner never saw does not fail
# a build, does not fail a typecheck and does not fail jsdom — it silently renders the diagram as
# one unstyled column. So the class names are grepped out of the compiled stylesheet.
set -uo pipefail

WEB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../decile-blueprint/apps/web" && pwd)"
cd "$WEB" || exit 1

LOG="${TMPDIR:-/tmp}/baskfy-flow-build.log"

# `.next-build`, never `.next` — RUN-AND-TEST.md: a build into `.next` while a dev server owns it
# leaves a mixed tree and the dev server serving chunks that no longer exist.
DIST=.next-build

if ! BASKFY_WEB_DIST_DIR="$DIST" pnpm exec next build >"$LOG" 2>&1; then
  echo "FLOW-RESPONSIVE FAIL: build"
  tail -20 "$LOG"
  exit 1
fi

CSS="$(find "$DIST/static/css" -name '*.css' 2>/dev/null | head -1)"
if [ -z "$CSS" ]; then
  echo "FLOW-RESPONSIVE FAIL: no stylesheet emitted"; exit 1
fi

# Every arbitrary utility the rebuilt stage depends on, and the one custom class that carries the
# animation's period. Fixed-string matches against the minified sheet.
MISSING=0
for CLASS in 'grid-cols-\[minmax' 'lg\:hidden' 'lg\:inline' 'xl\:grid-cols-4' \
             'text-\[12px\]' 'sm\:text-\[13px\]' 'xl\:text-\[13px\]' 'max-w-\[22ch\]' \
             'tracking-\[var' 'xl\:max-w-\[84rem\]' '.flow-stage{--flow-cycle:11s}' '@keyframes flow-segment' \
             '@keyframes flow-sweep' '@keyframes flow-ack'; do
  if ! grep -q -F -- "$CLASS" "$CSS"; then
    echo "MISSING FROM $CSS: $CLASS"
    MISSING=$((MISSING + 1))
  fi
done

if [ "$MISSING" -gt 0 ]; then
  echo "FLOW-RESPONSIVE FAIL: $MISSING utilities never reached the stylesheet"; exit 1
fi

echo "FLOW-RESPONSIVE OK (build green, 14 utilities emitted into $(basename "$CSS"))"

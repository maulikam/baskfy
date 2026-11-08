#!/usr/bin/env bash
# Tree 3b / G1 — which redirect stubs are dead code, and which are the mechanism.
#
# Next runs `redirects()` from next.config.ts BEFORE the filesystem routes, so a page file at a
# redirected path can never render. A stub at a path with NO config redirect is the opposite: it
# is the only thing making that URL work. That distinction is the whole task, so it is computed
# from the two sources of truth rather than from a list someone typed.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
exec python3 - "$@" <<'PY'
import pathlib, re, sys

web = pathlib.Path("decile-blueprint/apps/web")
app = web / "src/app/(app)"

# `{ source: "/screens/:id/columns", ... }` -> "/screens/[id]/columns", the page path it shadows.
def to_page_route(source: str) -> str:
    parts = []
    for seg in source.strip("/").split("/"):
        if seg.startswith(":"):
            parts.append("[" + seg[1:].rstrip("*") + "]")
        else:
            parts.append(seg)
    return "/" + "/".join(parts)

config = (web / "next.config.ts").read_text()

# ONLY the redirects() block. `headers()` carries a catch-all `{ source: "/:path*" }` for the
# security headers, and reading it as a redirect marks every route in the app as shadowed —
# which would have deleted the three files that are the only thing making their URL work.
block = re.search(r"redirects\(\)\s*\{(.*?)\n  \}", config, re.S)
if not block:
    sys.exit("could not find the redirects() block in next.config.ts")
sources = re.findall(r'\{\s*source:\s*"([^"]+)"', block.group(1))
redirected = {to_page_route(s) for s in sources}
# `/investments/:path*` shadows every page beneath /investments, not one exact path.
prefixes = {to_page_route(s).rsplit("/", 1)[0] for s in sources if ":path*" in s}

dead, load = [], []
for f in sorted(app.rglob("page.tsx")):
    body = f.read_text()
    if not re.search(r"\bredirect\(", body):
        continue
    if re.search(r"<[A-Za-z]", body):          # renders something -> not a stub
        continue
    route = "/" + str(f.parent.relative_to(app))
    shadowed = route in redirected or any(
        route == p or route.startswith(p + "/") for p in prefixes)
    (dead if shadowed else load).append(route)

print(f"DEAD — shadowed by a next.config redirect, unreachable, safe to delete ({len(dead)}):")
for r in dead:
    print(f"    {r}")
print(f"LOAD-BEARING — no config redirect, the page IS the mechanism, MUST keep ({len(load)}):")
for r in load:
    print(f"    {r}")
print()
print(f"INVENTORY dead={len(dead)} loadbearing={len(load)}")
PY

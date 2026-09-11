#!/usr/bin/env bash
# G7 — no secret is committed, and the deployed config keeps the root CLAUDE.md safety rails.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$ROOT" || exit 1

# 1. Nothing secret is tracked. `git ls-files` is the question that matters — a file can be on
#    disk and ignored, which is fine; tracked is not.
#
#    RESOLVED 2 Sep 2026 (NEEDS-MAULIK §20). This check found a Fernet key committed in 44c029c
#    (22 Aug 2026) and present in origin/developer, and carried a by-name exemption for it because
#    untracking a key without rotating it looks like a fix while changing nothing.
#
#    Both halves are now done, in the order that makes the second honest. Baskfy's own token store
#    was re-keyed and verified — the old key no longer decrypts it, and `api` and `worker` both
#    read the new one against a live Kite 200. The desk's store, dead since M70 (its token was
#    issued 22 Aug and Kite ends a session overnight), was backed up outside the repo and deleted,
#    key and ciphertext together. So the exemption is gone rather than widened: the key in git
#    history now decrypts a file that no longer exists.
#
#    What history still holds is a dead key. Removing it needs `git filter-repo` and a force-push,
#    which is a separate, coordinated decision and is not what this check is for.
TRACKED="$(git ls-files | grep -E '(^|/)\.env($|\.)|\.pem$|\.key$|\.env\.staging' \
  | grep -v '\.example$' || true)"
[ -z "$TRACKED" ] || fail "tracked secret-shaped files: $TRACKED"

for f in .env.staging .env.staging.compose; do
  [ -e "$f" ] || continue
  git check-ignore -q "$f" || fail "$f exists but is NOT gitignored — one 'git add -A' from history"
done

# 2. No literal secret in anything committed under infra/ or tools/deploy.
# The pattern requires the 53-character body, not just the `$2a$14$` prefix. Matching the prefix
# alone flagged two explanatory comments that write `$2a$14$…` — with an ellipsis — to explain why
# the `$` has to be escaped for Compose. This check therefore failed on documentation, and had
# been failing since before M74; a safety check that is always red is one nobody reads.
if git ls-files 'decile-blueprint/infra/**' 'tools/deploy/**' \
     | xargs grep -lE '\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}' 2>/dev/null | grep -q .; then
  fail "a bcrypt hash is committed"
fi

# 3. The safety rails, in the file that describes the box.
C="$BLUE/infra/docker/compose.prod.yml"
# c51f46b parameterised this as `${BASKFY_DRY_RUN:-true}` so the box sets it from its env file
# instead of diverging from git by hand. The rail is that the DEFAULT is true — a literal
# `DRY_RUN: "true"` has not existed since, so this check had been failing on its own convention.
grep -qE 'DRY_RUN: "(true|\$\{BASKFY_DRY_RUN:-true\})"' "$C" \
  || fail "DRY_RUN does not default to true in compose.prod.yml"
grep -q 'BASKFY_PUBLIC_API_ENABLED: "false"' "$C" || fail "the public API is not shut (D9)"
grep -q 'BASKFY_FREE_TIER_ENABLED: "false"'  "$C" || fail "a Track B flag is not false"
grep -q 'DRY_RUN=true' "$BLUE/infra/docker/Dockerfile.python" || fail "DRY_RUN is not baked into the image"

# 3b. VB13 — the volume-breakout sleeve's rails, as compose defaults.
#
# `BASKFY_VBT_EXECUTION_ENABLED` must default false wherever it is named, and it must be **named**
# — a variable this file does not name never reaches the container, so an env file that sets it
# would look like a decision and do nothing. That was the sleeve's state until 11 Sep 2026.
#
# And there must be no auto-execute for it, here or anywhere: non-negotiable 1's named exception
# belongs to the swing sleeve alone. The repository-wide version of this check is
# `kite-momentum-rebalancer/tests/test_vbt_safety.py`; this is the deployed-config half.
grep -q 'BASKFY_VBT_EXECUTION_ENABLED: "${BASKFY_VBT_EXECUTION_ENABLED:-false}"' "$C"   || fail "BASKFY_VBT_EXECUTION_ENABLED is not named-and-false in compose.prod.yml"
# Comment lines are stripped first: the block above states the prohibition in prose
# ("THERE IS NO BASKFY_VBT_AUTO_EXECUTE"), and a prohibition must not trip the check that
# states it. A real assignment survives the strip and still fails.
! sed 's/^[[:space:]]*#.*$//' "$C" | grep -qiE 'VBT[_A-Z]*AUTO[_A-Z]*EXECUTE' \
  || fail "compose.prod.yml sets a VBT auto-execute flag — there is no such thing"

# 4. Postgres and Redis must not be published to the host. On a box with a public EIP, 5432 is
#    the most-scanned port on the internet.
python3 - "$C" <<'PY' || exit 1
import re, sys
text = open(sys.argv[1]).read()
for svc in ("postgres:", "redis:"):
    block = text.split("\n  " + svc, 1)[1].split("\n  ", 1)[0] if "\n  " + svc in text else ""
    if re.search(r'^\s+ports:', block, re.M):
        print(f"FAIL: {svc[:-1]} publishes a port to the host", file=sys.stderr)
        raise SystemExit(1)
PY

echo "SAFETY OK — no tracked secrets, DRY_RUN true, VBT execution false and no auto-execute, public API shut, data ports unpublished"

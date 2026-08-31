# Gates: 1.1.3 Credential inventory and rotation list

Scope: enumerate every credential the cutover touches, its current location, and whether it
needs rotating. No secret values in any output.

- [x] G1: Every Kite/Google/session credential is listed with location and status
  CHECK: grep -c "KITE\|GOOGLE\|ENCRYPTION" NEEDS-MAULIK.md
  EXPECT: /[1-9]/
  EVIDENCE: `grep -c "KITE\|GOOGLE\|ENCRYPTION" NEEDS-MAULIK.md` = 34 (31 Aug 2026).
    NEEDS-MAULIK.md §29 tables 19 credentials across three hosts (desk / box / laptop): Kite
    Connect KITE_API_KEY, KITE_API_SECRET, TOKEN_FILE, BASKFY_KITE_API_KEY,
    BASKFY_KITE_API_SECRET, BASKFY_KITE_TOKEN_ENCRYPTION_KEY, BASKFY_KITE_TOKEN_PATH,
    BASKFY_KITE_PUBLISHER_API_KEY; M58 bridge BASKFY_KITE_DESK_SSH_TARGET,
    BASKFY_KITE_DESK_SSH_KEY_PATH, BASKFY_KITE_DESK_KNOWN_HOSTS_PATH + the desk authorized_keys
    line; Google BASKFY_GOOGLE_CLIENT_ID, BASKFY_GOOGLE_CLIENT_SECRET; session AUTH_SECRET,
    BASKFY_JWT_SECRET, DESK_PASSWORD, BASKFY_METRICS_TOKEN. Live status measured per container
    on the box via `AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh` running
    `printenv "$v" | wc -c` — UNSET / SET_EMPTY / SET len=N, never a value. Sources read:
    decile-blueprint/packages/providers/src/baskfy_providers/settings.py,
    decile-blueprint/services/api/src/baskfy_api/settings.py,
    kite-momentum-rebalancer/app/config.py:9-40, .env.example:56-65,260-263,290,386-387.

- [x] G2: The four secrets known to have been exposed in agent transcripts are named as
      requiring rotation (Google client secret, RENIL Kite api_secret, and the Kite api_key +
      token encryption key printed on 2026-08-31)
  CHECK: grep -ci "rotate" NEEDS-MAULIK.md
  EXPECT: /[1-9]/
  EVIDENCE: `grep -ci rotate NEEDS-MAULIK.md` = 11 (31 Aug 2026). §29.2 names all four as
    R1 KITE_API_SECRET / BASKFY_KITE_API_SECRET (RENIL) — 27 Aug transcript;
    R2 BASKFY_KITE_TOKEN_ENCRYPTION_KEY — 31 Aug transcript;
    R3 KITE_API_KEY / BASKFY_KITE_API_KEY — 31 Aug transcript;
    R4 BASKFY_GOOGLE_CLIENT_SECRET — 27 Aug transcript.
    The 27 Aug pair is quoted from docs/00-merge-status.md:1183-1187 ("Two secrets pasted into
    an agent transcript on 27 Aug still need rotating: the Google client secret and the RENIL
    Kite api_secret"). Each row states what it is, why it must rotate and what breaks; the
    order is R1 → R2 → R4 → R3, driven by leaf 1.1.1 (the box must never receive the exposed
    secret). R3 is recorded as not regenerable — the api_key is the Kite app's identity and is
    public by design — with the recommendation to take no action, R1's rotation killing the
    exposed pair. The desk access token printed on 30 Aug is recorded as already retired: the
    box's blob at /var/lib/baskfy/state/kite-token.enc has mtime 31 Aug 16:17.

- [x] G3: `BASKFY_KITE_API_SECRET` is recorded as required-or-not, resolved against 1.1.1's
      chosen login path
  EVIDENCE: NEEDS-MAULIK.md §29.3. Measured state: `BASKFY_KITE_API_SECRET=SET_EMPTY` in both the
    `worker` and `api` containers on the box (box.sh + `printenv | wc -c`, 31 Aug 2026); the
    laptop's decile-blueprint/.env holds a 32-char value, the box does not.
    `test -f docs/DESK-LOGIN-DECISION.md` = FALSE at write time — leaf 1.1.1 had not landed its
    decision, so BOTH branches are recorded, as the leaf brief instructs:
      Branch A (Baskfy owns the redirect) — REQUIRED. The box redeems the request_token at
      /api/v1/brokers/callback; `kite_configured()` in providers/settings.py returns False
      without it, so the login cannot complete. This is the LIVE state: gates/desk-retire-1.1.1.md
      records the redirect already changed to https://staging.baskfy.com/api/v1/brokers/callback.
      Branch B (desk owns the redirect, Baskfy uses the M58 token bridge) — NOT required and
      should stay empty; the box never redeems anything, it pulls the desk's already-redeemed
      access token over SSH (docs/DECISIONS-MERGE.md M58, line 5795).
    Either way the value written must be the ROTATED one (R1), not the exposed value now on the
    laptop. Re-read §29.3 against docs/DESK-LOGIN-DECISION.md once 1.1.1 lands it.

- [x] G4: No secret value appears anywhere in the leaf's output or committed files
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git diff --cached NEEDS-MAULIK.md 2>/dev/null | grep -cE "[a-z0-9]{16,}=|api_secret=[^ ]" || echo 0
  EXPECT: 0
  EVIDENCE: `git diff --cached NEEDS-MAULIK.md | grep -cE "[a-z0-9]{16,}=|api_secret=[^ ]"` = 0
    (file staged, 31 Aug 2026). Independently verified stronger: a python scan compared every
    env value of length >= 8 from .env.staging, decile-blueprint/.env and
    kite-momentum-rebalancer/.env (48 values) against the full text of NEEDS-MAULIK.md — the
    only two substring hits were NEXT_PUBLIC_SITE_URL (a public URL) and TOKEN_FILE
    (`data/.kite_token.json`, a path); neither is a secret. A high-entropy-token scan over §29
    returned only file paths and URLs. Status on the box was established throughout with
    `printenv "$v" | wc -c`, which emits UNSET / SET_EMPTY / SET len=N and never a value.

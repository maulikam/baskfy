"""Pull the day's Kite access token from the Mumbai box (M18+, point 2).

WHY THIS EXISTS
---------------
The Kite app's Redirect URL is `https://desk.modelbasket.in/callback`, and it stays that way. The
login has to land on the box, so the token is minted there — but the merge's pipeline work runs on
the laptop, and it needs the same token to reach Kite for historical bars and instruments.

Rather than register a second redirect (a change to a live trading app, for a development
convenience), this fetches the token the box already holds over the SSH connection
`deploy/sync.sh` already uses, and writes it into the laptop's **encrypted** store.

    make token-sync TARGET=desk@1.2.3.4

THE TOKEN IS NEVER PRINTED. Not to stdout, not to the log, not in an error. The verification is one
cheap authenticated call — `kc.profile()` — and all it reports is whether the token works and which
`user_type` answered.

TWO FORMATS, BECAUSE THE BOX IS BEHIND
--------------------------------------
The box runs the pre-M16 layout until its next deliberate deploy, so its token file is **plain
JSON**. After that deploy it will be a Fernet blob. This reads either: JSON first, and if that
fails, the encrypted store using the key the box would have used. The laptop's copy is always
written encrypted regardless.

WHAT THIS DOES NOT DO
---------------------
It does not log in. Kite mints a token per day, per human, through a browser, and nothing here
removes that step — `NEEDS-MAULIK.md` says so plainly. This moves a token that already exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_REMOTE_DIR = "/home/desk/kite-momentum-rebalancer"
DEFAULT_REMOTE_TOKEN = "data/.kite_token.json"


def _fetch(target: str, remote_path: str) -> bytes:
    """Read the remote token file, as bytes, without it touching a shell echo or a log.

    The app runs as `desk` and the token is 0600, so an admin login (`ubuntu`) cannot read it
    directly. Plain `cat` is tried first — if the far side is already the owner, no sudo is
    involved at all — and only a permission failure escalates to `sudo -n`. `-n` means a box
    that would prompt for a password fails loudly instead of hanging on an invisible prompt.
    """
    for cmd in (f"cat {remote_path}", f"sudo -n cat {remote_path}"):
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", target, cmd], capture_output=True, check=False
        )
        if proc.returncode == 0:
            if not proc.stdout.strip():
                raise SystemExit(f"{remote_path} on {target} is empty — has anyone logged in?")
            return proc.stdout
        err = proc.stderr.decode("utf-8", "replace").strip()
        if "Permission denied" not in err and "denied" not in err.lower():
            break
    raise SystemExit(
        f"could not read {remote_path} on {target}: {err or 'ssh failed'}\n"
        f"Check the box is reachable, that a login has happened (the file only exists after "
        f"one), and that this account may sudo without a password."
    )


def _extract(blob: bytes, remote_key: str) -> str:
    """Plain JSON (the box's current format) or a Fernet blob (after the box is updated)."""
    try:
        parsed = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    else:
        token = parsed.get("access_token", "")
        if token:
            return str(token)
        raise SystemExit("the remote token file is JSON but carries no access_token")

    if not remote_key:
        raise SystemExit(
            "the remote token is encrypted and no --remote-key was given. Pass the box's "
            "KITE_TOKEN_ENCRYPTION_KEY, or read it from the box's .env."
        )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "token.enc"
        path.write_bytes(blob)
        from baskfy_providers.tokens import AccessTokenStore  # noqa: PLC0415

        return AccessTokenStore(path, remote_key).load().value


def _verify(api_key: str, token: str) -> str:
    """One cheap authenticated call. Returns a description; never the token.

    `profile()` and nothing heavier: it costs no data quota, and unlike a bar-fetch probe
    it cannot fail for a reason unrelated to the token (see the data-tier note in NEEDS-MAULIK).
    """
    from kiteconnect import KiteConnect  # noqa: PLC0415

    kc = KiteConnect(api_key=api_key)
    kc.set_access_token(token)
    profile = kc.profile()
    return str(profile.get("user_type", "unknown"))


def _fingerprint(value: str) -> str:
    """Enough to compare two secrets without either of them being shown."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _remote_api_key_fingerprint(target: str, remote_dir: str) -> str:
    """Ask the box for its api_key's fingerprint, so a failure can name its own cause.

    Kite returns one message — "Incorrect `api_key` or `access_token`" — for two very different
    problems. If the fingerprints match, the key is not the issue and the token is simply stale.
    """
    # sha256sum on the far side rather than a nested python -c: one level of shell quoting is
    # survivable, two is how you end up fingerprinting the empty string and blaming the wrong thing.
    # pipefail is not decoration: without it a permission-denied grep still exits 0 through cut, and
    # the caller confidently reports a key mismatch that is really sha256 of nothing.
    script = (
        f"grep -h '^KITE_API_KEY' {remote_dir}/.env | cut -d= -f2- "
        f"| tr -d '\\r\\n' | sha256sum | cut -c1-12"
    )
    quoted = shlex.quote(script)
    for cmd in (f"bash -o pipefail -c {quoted}", f"sudo -n bash -o pipefail -c {quoted}"):
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", target, cmd], capture_output=True, check=False
        )
        out = proc.stdout.decode().strip()
        if proc.returncode == 0 and out:
            return out
    return "unreadable"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True, help="e.g. desk@1.2.3.4, as deploy/sync.sh takes")
    ap.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    ap.add_argument("--remote-token", default=DEFAULT_REMOTE_TOKEN)
    ap.add_argument("--remote-key", default="", help="only if the box stores it encrypted")
    args = ap.parse_args()

    # These imports are deferred, not lazy: `app` is only importable once the repo root
    # is on the path, and that happens on the line above.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import config as C  # noqa: PLC0415
    from app.token_store import store_for  # noqa: PLC0415

    blob = _fetch(args.target, f"{args.remote_dir}/{args.remote_token}")
    token = _extract(blob, args.remote_key)

    try:
        # Verify BEFORE storing: a dead token must never replace a live one on disk.
        user_type = _verify(C.KITE_API_KEY, token)
    except Exception:  # every auth failure gets diagnosed and re-raised as a clear exit
        remote_fp = _remote_api_key_fingerprint(args.target, args.remote_dir)
        local_fp = _fingerprint(C.KITE_API_KEY)
        if remote_fp == local_fp:
            cause = (
                "The api_key matches the box's, so the key is not the problem: the token on the "
                "box has expired. Kite tokens die at ~06:00 IST the morning after they are minted, "
                "so a token from yesterday evening is already dead. Log in once at "
                "https://desk.modelbasket.in/ and run this again."
            )
        else:
            cause = (
                f"The api_key does NOT match the box's (local {local_fp}, box {remote_fp}). The "
                f"laptop's .env has drifted from the box's — fix that before blaming the token."
            )
        raise SystemExit(f"the token from {args.target} did not authenticate.\n\n{cause}") from None

    store_for(C.TOKEN_FILE).save(token)  # reads KITE_TOKEN_ENCRYPTION_KEY from the env itself

    print(f"token pulled from {args.target} and stored encrypted at {C.TOKEN_FILE}")
    print(f"verified with one profile() call — user_type={user_type}")
    print("the token itself was not printed and is not in any log line above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

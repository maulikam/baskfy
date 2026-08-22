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

TWO STORES ON THIS SIDE, AND BOTH GET WRITTEN
---------------------------------------------
The merged repo has two consumers of the same daily token and they do **not** share a file:

* the **desk** reads `app.config.TOKEN_FILE` (`kite-momentum-rebalancer/data/.kite_token.json`);
* the **screener's pipeline** reads `BASKFY_KITE_TOKEN_PATH`
  (`decile-blueprint/.secrets/kite-token.enc` by default), which is what
  `baskfy_worker.backfill` and `providers doctor` look at.

Until 22 Aug 2026 this script wrote only the first, while `NEEDS-MAULIK.md` item 3 claimed the
bridge unblocked `baskfy_worker.backfill`. It did not: after a perfectly successful sync,
`make doctor` still reported `[DOWN] kite — no encrypted access token at .secrets/kite-token.enc`.
One login now feeds both, because the alternative is a bridge that reports success and leaves the
half the pipeline needs empty.

The two stores use different encryption keys by design — each side reads its own
`KITE_TOKEN_ENCRYPTION_KEY` — so this writes the plaintext token into each store separately
rather than copying one blob to two paths.

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

#: The screener tree, relative to this repo's root. Both live under `baskfy/` after M1.
DEFAULT_SCREENER_DIR = "../decile-blueprint"
#: `ProviderSettings.kite_token_path`'s default, repeated here so a missing .env still resolves.
DEFAULT_SCREENER_TOKEN_PATH = ".secrets/kite-token.enc"

#: Exit code for "the desk store was written, the screener store was not". Distinct from 1 so a
#: caller can tell a half-success from a failure — the desk is usable, the pipeline is not.
EXIT_SCREENER_NOT_WRITTEN = 3


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


def _env_value(env_path: Path, key: str) -> str:
    """One value out of a .env file, without importing anything that resolves a *different* .env.

    `ProviderSettings` reads `.env` relative to the current working directory, and this script
    runs from the desk's root — so asking pydantic would hand back the desk's file and silently
    encrypt the screener's blob with the wrong key. Reading the named file is the whole point.

    Deliberately literal: no interpolation, no export prefix, no quoting rules beyond stripping a
    matched pair. The screener's .env is generated from `.env.example` and never uses them.
    """
    if not env_path.is_file():
        return ""
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return ""


def _write_screener_store(token: str, screener_dir: Path) -> tuple[Path | None, str]:
    """Write the same token into the screener's encrypted store. Returns (path, reason-if-not).

    Never raises: the desk's copy is already on disk by the time this runs, and turning a
    configuration gap in the *other* tree into a traceback would throw away a good sync. The
    caller reports the reason and exits non-zero instead.
    """
    if not screener_dir.is_dir():
        return None, f"{screener_dir} does not exist"

    env_path = screener_dir / ".env"
    key = _env_value(env_path, "BASKFY_KITE_TOKEN_ENCRYPTION_KEY")
    if not key:
        return None, (
            f"BASKFY_KITE_TOKEN_ENCRYPTION_KEY is not set in {env_path}. Generate one with\n"
            '        python -c "from cryptography.fernet import Fernet; '
            "print(Fernet.generate_key().decode())\"\n"
            "    and add it there; the screener refuses to store a token unencrypted."
        )

    relative = _env_value(env_path, "BASKFY_KITE_TOKEN_PATH") or DEFAULT_SCREENER_TOKEN_PATH
    target = Path(relative)
    if not target.is_absolute():
        target = screener_dir / target

    from baskfy_providers.tokens import AccessTokenStore  # noqa: PLC0415

    try:
        AccessTokenStore(target, key).save(token)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed; see the docstring
        return None, f"{type(exc).__name__}: {exc}"
    return target, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True, help="e.g. desk@1.2.3.4, as deploy/sync.sh takes")
    ap.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    ap.add_argument("--remote-token", default=DEFAULT_REMOTE_TOKEN)
    ap.add_argument("--remote-key", default="", help="only if the box stores it encrypted")
    ap.add_argument(
        "--screener-dir",
        default=DEFAULT_SCREENER_DIR,
        help="the screener tree whose .secrets store also needs the token",
    )
    ap.add_argument(
        "--no-screener",
        action="store_true",
        help="write only the desk's store (the pre-22-Aug behaviour)",
    )
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

    screener_path: Path | None = None
    reason = "skipped by --no-screener"
    if not args.no_screener:
        repo_root = Path(__file__).resolve().parent.parent
        screener_dir = Path(args.screener_dir)
        if not screener_dir.is_absolute():
            screener_dir = (repo_root / screener_dir).resolve()
        screener_path, reason = _write_screener_store(token, screener_dir)

    if screener_path is not None:
        print(f"the screener's pipeline store was written too: {screener_path}")
    print("the token itself was not printed and is not in any log line above.")

    if screener_path is None and not args.no_screener:
        print(
            f"\nWARNING: the desk can trade, but the pipeline cannot fetch bars.\n"
            f"    {reason}\n"
            f"    `make backfill` and `make doctor` will report kite as DOWN until this is fixed.",
            file=sys.stderr,
        )
        return EXIT_SCREENER_NOT_WRITTEN
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

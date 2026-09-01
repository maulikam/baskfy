"""Deposit a Kite access token into Baskfy's store — the bridge around one redirect URL.

    python -m baskfy_worker.kite_session_cli deposit --token <access_token>
    python -m baskfy_worker.kite_session_cli status

THE PROBLEM THIS SOLVES
=======================
A Kite Connect app has exactly **one** registered redirect URL, and the RENIL app's points at
``https://desk.modelbasket.in/callback`` — the momentum desk, which places live orders and cannot
lose its login. Repointing it at Baskfy would break the desk; Zerodha will not issue a second app
for the same purpose. So Baskfy can never *start* a Kite login of its own.

It does not need to. ``KiteProvider`` performs no OAuth: it reads an access token out of
``AccessTokenStore`` and calls the API with it (`providers/kite.py`). The desk already obtains
exactly that token every morning, through the redirect it owns. What was missing was a way to put
it where Baskfy looks.

**So the bridge carries the access token, not the login.** One app, one redirect, one login — and
two consumers of the session it produces. Kite permits the same access token from several
processes; nothing here logs in, races the desk, or invalidates its session.

WHY NOT THE REQUEST TOKEN
-------------------------
The obvious alternative — have the operator copy the ``request_token`` out of the callback URL and
let Baskfy exchange it — cannot work. The desk's own ``/callback`` consumes it on arrival, and a
Kite request token is single-use. By the time a human could copy it, it is spent.

WHAT THIS UNLOCKS
-----------------
``fetch_daily_bars`` via Kite (the pipeline otherwise falls back to the bhavcopy, which only
reaches 2024), the deep historical backfill from 2011 (docs/08 D5), and broker holdings sync.

OPERATIONALLY
-------------
A Kite access token expires overnight with no refresh, so this runs once per trading day, after
the desk's login. It is deliberately a command rather than an endpoint: the token is a live
credential for an account that can trade, and an HTTP route that accepts one is a route that can
be tricked into accepting one. `docs/DECISIONS-MERGE.md` M57.

`deposit` still requires a human with the token in hand. `pull` removes the human: it asks the
desk for the token over SSH, using a key the desk has bound to a *forced command* that emits the
token and nothing else. So the credential Baskfy holds is not "a login on the trading box" — it
is "the right to read one string from it". That distinction is the whole security argument, and
it is enforced by the desk's sshd rather than by this code. `docs/DECISIONS-MERGE.md` M58.

**Daily data does not depend on any of this.** `fetch_daily_bars` falls back to the NSE bhavcopy,
which needs no credential and no login. The Kite session buys history before 2024, holdings sync,
and instrument metadata. A failed pull is therefore a warning, never a stopped pipeline.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence

import httpx

from baskfy_providers.errors import CredentialsMissing
from baskfy_providers.settings import get_provider_settings
from baskfy_providers.tokens import AccessTokenStore


def _store() -> AccessTokenStore:
    settings = get_provider_settings()
    if not settings.kite_token_encryption_key:
        raise SystemExit(
            "BASKFY_KITE_TOKEN_ENCRYPTION_KEY is not set; refusing to write a live broker "
            "credential to disk unencrypted."
        )
    return AccessTokenStore(settings.kite_token_path, settings.kite_token_encryption_key)


def deposit(token: str) -> int:
    """Store *token*, then read it straight back to prove the round trip.

    Verified rather than assumed: an encryption key that does not match the one the provider
    reads with would store happily and fail at the next fetch, hours later, as an unexplained
    provider error. Better to fail here, in front of the person who ran the command.
    """
    store = _store()
    record = store.save(token.strip())
    reloaded = store.load()
    if reloaded.value != record.value:
        raise SystemExit("stored token did not read back identically; refusing to report success")

    # Never the token itself — this prints to a terminal and often into a scrollback.
    print(f"stored: {len(record.value)} chars, issued_at={record.issued_at.isoformat()}")
    print(f"path  : {store.path}")
    return 0


#: What a Kite access token may contain. Zerodha documents no format, so this is deliberately
#: loose on length and strict on charset — its job is to reject the *other* thing that can arrive
#: on stdout: a shell's error message, an MOTD line, a stack trace. Storing one of those would
#: look like success here and fail hours later as an unexplained provider error.
_TOKEN_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{16,128}\Z")


def fetch_desk_token() -> str:
    """Ask the desk for today's access token over SSH.

    The command is built as a list and run without a shell, so the token never passes through one.
    It arrives on stdout — not in an argument — because a command line is world-readable in
    `ps` for as long as the process lives.
    """
    settings = get_provider_settings()
    if not settings.desk_session_pull_configured():
        raise SystemExit(
            "BASKFY_KITE_DESK_SSH_TARGET is not set; this deployment cannot pull a session. "
            "Use `deposit --token` for a one-off."
        )

    command = [
        "ssh",
        "-T",  # no pty: the desk's forced command writes one line and exits
        "-n",  # never read our stdin
        "-i",
        settings.kite_desk_ssh_key_path,
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",  # fail rather than prompt; a nightly job has nobody to answer
        "-o",
        "PasswordAuthentication=no",
        "-o",
        # Pinned, never accept-new. An unattended job cannot judge an unfamiliar host key.
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={settings.kite_desk_known_hosts_path}",
        "-o",
        f"ConnectTimeout={int(settings.kite_desk_ssh_timeout_seconds)}",
        settings.kite_desk_ssh_target,
        # Ignored: the desk binds this key to a forced command. Sent anyway so that anyone
        # reading a process list sees what was intended, and so the call still works if the
        # forced command is ever replaced by an explicit one.
        "kite-access-token",
    ]

    try:
        # Fixed argv, no shell, nothing interpolated from user input.
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=settings.kite_desk_ssh_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"the desk did not answer within {exc.timeout}s") from exc
    except FileNotFoundError as exc:  # no ssh binary in the image
        raise SystemExit("no ssh client on this host; the image needs openssh-client") from exc

    if completed.returncode != 0:
        # stderr only. stdout is where the token would be, and echoing it on failure is exactly
        # how a credential ends up in a log.
        detail = completed.stderr.strip().splitlines()[-1:] or ["no detail"]
        raise SystemExit(
            f"desk refused or was unreachable (exit {completed.returncode}): {detail[0]}"
        )

    token = completed.stdout.strip()
    if not _TOKEN_PATTERN.match(token):
        # Deliberately describes the shape rather than quoting it back.
        raise SystemExit(
            f"the desk returned {len(token)} characters that do not look like an access token; "
            "refusing to store it"
        )
    return token


#: Kite's cheapest authenticated call. Chosen because it reads nothing and changes nothing — a
#: liveness probe should not be able to affect the account it is probing.
_PROFILE_URL = "https://api.kite.trade/user/profile"


def verify_session(token: str) -> str:
    """Prove the token actually works, and return the Kite user id it belongs to.

    Two failures this separates, which otherwise look identical hours later in a provider error:

    * **The desk has not logged in today.** Its token file still holds yesterday's string, which
      reads back perfectly and is dead. Storing it would leave the pipeline believing it has a
      session.
    * **This host is not whitelisted on the Kite app.** Nothing about the token is wrong; the
      call is refused because of where it came from. Only a call *from this box* can tell the
      difference, which is why the check lives here and not on the desk.
    """
    settings = get_provider_settings()
    if not settings.kite_api_key:
        raise SystemExit("BASKFY_KITE_API_KEY is not set; a token alone cannot call Kite")
    try:
        response = httpx.get(
            _PROFILE_URL,
            headers={
                "X-Kite-Version": "3",
                # The token is a header value, not a query parameter: query strings are logged by
                # proxies and appear in referrers.
                "Authorization": f"token {settings.kite_api_key}:{token}",
            },
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise SystemExit(
            f"could not reach Kite to verify the session: {type(exc).__name__}"
        ) from exc

    if response.status_code == httpx.codes.FORBIDDEN:
        raise SystemExit(
            "Kite refused the token (403). Either the desk has not logged in today, or this "
            "host's IP is not whitelisted on the Kite app."
        )
    if response.status_code != httpx.codes.OK:
        raise SystemExit(f"Kite answered {response.status_code} when verifying the session")

    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    user_id = data.get("user_id") if isinstance(data, dict) else None
    if not isinstance(user_id, str) or not user_id:
        raise SystemExit("Kite's profile response carried no user_id; refusing to trust it")
    return user_id


def pull(verify: bool = True) -> int:
    """Fetch the desk's token, prove it works, and store it.

    Verified **before** storing rather than after. A token that fails the check is not better than
    no token — both leave the pipeline without a Kite session — but a stored dead one also hides
    the reason, and the reason is the only actionable part.
    """
    token = fetch_desk_token()
    if verify:
        user_id = verify_session(token)
        print(f"verified: live Kite session for {user_id}")
    return deposit(token)


def local_session_is_live() -> bool:
    """Does Baskfy already hold a Kite session that works right now?

    Presence is not the question — a Kite access token dies overnight, so yesterday's blob reads
    back perfectly and is dead. This asks Kite, which is the only thing that can answer.
    """
    try:
        store = _store()
        if not store.exists():
            return False
        verify_session(store.load().value)
    except (SystemExit, CredentialsMissing):
        return False
    return True


def refresh_quietly() -> bool:
    """Make sure a Kite session exists before the nightly runs. Baskfy's own comes first.

    **Why this checks locally before asking the desk (M75).** This used to call :func:`pull`
    unconditionally, which fetches from the momentum desk. That was right while the desk owned the
    Kite redirect. M70-M73 moved the login to Baskfy at Maulik's instruction, so the desk no longer
    logs in — and every night since, this has reported "Kite refused the token (403). Either the
    desk has not logged in today, or this host's IP is not whitelisted", which reads like an
    infrastructure fault and is actually a design change nobody told this function about.

    Meanwhile Baskfy's own Connect button had already written a working session to the very store
    the pipeline reads. So the first question is whether we already have one; the desk is now the
    fallback, not the source.

    **This is no longer as best-effort as its old docstring claimed.** That text said a missing
    session "does not cost the day's bars, which come from the bhavcopy". On the Phase-A box NSE
    answers 403 to every request — the bhavcopy fallback does not exist there — so Kite is the
    only path to a bar and a missing session costs the entire night. It still returns rather than
    raises, because the failure belongs to the step that needs the session, with the instrument it
    was fetching; but a `False` here means the night is in trouble, not merely thinner.
    """
    if local_session_is_live():
        return True
    try:
        pull()
    except SystemExit as exc:
        print(f"kite session not refreshed: {exc}", file=sys.stderr)
        return False
    return True


def status() -> int:
    """Say whether a usable session exists, without revealing it."""
    store = _store()
    if not store.exists():
        print(f"no token at {store.path}")
        return 1
    record = store.load()
    expired = record.is_expired()
    print(f"path      : {store.path}")
    print(f"issued_at : {record.issued_at.isoformat()}")
    print(f"expired   : {expired}")
    # A Kite token dies overnight; "present" and "usable" are different questions and conflating
    # them is how a pipeline discovers the problem at 6pm instead of at 9am.
    return 1 if expired else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m baskfy_worker.kite_session_cli", description=__doc__
    )
    sub = parser.add_subparsers(dest="command", required=True)
    dep = sub.add_parser("deposit", help="store an access token obtained by the desk's login")
    dep.add_argument(
        "--token",
        required=True,
        help="the access_token from the desk's Kite session (not the request_token)",
    )
    pul = sub.add_parser("pull", help="fetch today's token from the desk over SSH and store it")
    pul.add_argument(
        "--no-verify",
        action="store_true",
        help="store without calling Kite first (for a host with no outbound access to Kite)",
    )
    sub.add_parser("status", help="is there a usable Kite session, and how old is it")

    args = parser.parse_args(argv)
    if args.command == "deposit":
        return deposit(args.token)
    if args.command == "pull":
        return pull(verify=not args.no_verify)
    return status()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

#!/usr/bin/env python3
"""Prove, against the DEPLOYED desk's own source, that /callback updates the live process.

Leaf 1.1.1 established the hazard (its point (c)): `app/main.py` caches the Kite client in a
module global and `app/kite_client.py` reads the token file only in `Kite.__init__`, so a token
*written to disk* at 09:00 is never seen by the uvicorn process that places Friday's orders.
That is why the reverse bridge carries a `request_token` to the desk's own `/callback` instead of
writing a token file: `/callback` calls `kite().exchange_token(...)`, which calls
`set_access_token` on the cached client, in place.

This script demonstrates both halves as facts rather than as a reading of the code:

    1. the cache problem is REAL — a `Kite` built once never re-reads its token file;
    2. `exchange_token` updates that same cached object, and every reference already handed out
       (the order gateway holds `kite().kc`) sees the new token immediately;
    3. the deployed `app/main.py` really is wired that way — singleton, callback, gateway.

Run it ON the desk, from the app directory. It never touches the real token file, never reaches
Kite, and never constructs anything that can place an order: `kiteconnect` is replaced by a stub
in `sys.modules` before the app is imported.

    cd /home/desk/kite-momentum-rebalancer && \
      DRY_RUN=true .venv/bin/python /home/desk/bin/prove-live-token-pickup.py
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import tempfile
import types

APP_DIR = pathlib.Path(os.environ.get("DESK_APP_DIR", "/home/desk/kite-momentum-rebalancer"))

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


# --------------------------------------------------------------------- a KiteConnect that cannot
class StubKiteConnect:
    """Records what the app does to it. Reaches no network and places no order."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.access_token: str | None = None
        self.generated: list[str] = []

    def set_access_token(self, token: str) -> None:
        self.access_token = token

    def generate_session(self, request_token: str, api_secret: str) -> dict[str, str]:
        self.generated.append(request_token)
        return {"access_token": f"EXCHANGED-{request_token}"}


stub = types.ModuleType("kiteconnect")
stub.KiteConnect = StubKiteConnect  # type: ignore[attr-defined]
exceptions = types.ModuleType("kiteconnect.exceptions")


class _KiteException(Exception):
    pass


for _name in ("KiteException", "TokenException", "InputException", "NetworkException",
              "OrderException", "GeneralException", "DataException", "PermissionException"):
    setattr(exceptions, _name, type(_name, (_KiteException,), {}))
stub.exceptions = exceptions  # type: ignore[attr-defined]
sys.modules["kiteconnect"] = stub
sys.modules["kiteconnect.exceptions"] = exceptions

# --------------------------------------------------------------------- import the deployed code
scratch = pathlib.Path(tempfile.mkdtemp(prefix="desk-token-proof-"))
token_file = scratch / ".kite_token.json"
os.environ["TOKEN_FILE"] = str(token_file)
os.environ["DRY_RUN"] = "true"
os.environ["FORCE_IPV4"] = "false"
os.environ.setdefault("KITE_API_KEY", "stub-api-key")
os.environ.setdefault("KITE_API_SECRET", "stub-api-secret")
sys.path.insert(0, str(APP_DIR))

token_file.write_text(json.dumps({"access_token": "TOKEN-A"}), encoding="utf-8")

from app import config as C  # noqa: E402
from app.kite_client import Kite  # noqa: E402

check(
    "the proof runs against a scratch token file, never the desk's",
    str(C.TOKEN_FILE) == str(token_file),
    str(C.TOKEN_FILE),
)

# 1 -------------------------------------------------------------------- the token loads at init
k = Kite()
gateway_reference = k.kc  # what main.gateway() captures: OrderGateway(kite().kc, _risk)
check("a fresh Kite loads the token from disk", k.kc.access_token == "TOKEN-A")

# 2 ------------------------------------------------------- the cache problem, demonstrated
token_file.write_text(json.dumps({"access_token": "TOKEN-B"}), encoding="utf-8")
check(
    "a cached Kite NEVER re-reads the file (this is leaf 1.1.1's point (c), reproduced)",
    k.kc.access_token == "TOKEN-A",
    f"file says TOKEN-B, the live client still says {k.kc.access_token}",
)

# 3 ------------------------------------------- exchange_token updates the LIVE object in place
before_id = id(k.kc)
k.exchange_token("REQTOKEN")
check(
    "exchange_token updates the cached client without building a new one",
    k.kc.access_token == "EXCHANGED-REQTOKEN" and id(k.kc) == before_id,
    f"access_token={k.kc.access_token} same_object={id(k.kc) == before_id}",
)
check(
    "a reference handed out earlier (the order gateway's) sees it too",
    gateway_reference.access_token == "EXCHANGED-REQTOKEN",
)
check(
    "and the token file is rewritten by the desk's own code, in the desk's own format",
    json.loads(token_file.read_text())["access_token"] == "EXCHANGED-REQTOKEN",
)
check(
    "the deployed store is plain JSON with one key (NOT repo HEAD's Fernet)",
    sorted(json.loads(token_file.read_text())) == ["access_token"],
)

# 4 ------------------------------------------------------- the deployed main.py is wired that way
main_src = (APP_DIR / "app" / "main.py").read_text(encoding="utf-8")
check(
    "main.py caches the client in a module global",
    re.search(r"^_kite: Kite \| None = None$", main_src, re.M) is not None,
)
check(
    "main.kite() returns that singleton",
    re.search(r"def kite\(\) -> Kite:\s*\n\s*global _kite\s*\n\s*if _kite is None:", main_src)
    is not None,
)
check(
    "/callback drives exchange_token on it",
    "@app.get(\"/callback\")" in main_src and "kite().exchange_token(request_token)" in main_src,
)
check(
    "the order gateway is constructed from kite().kc, so it shares the object",
    "OrderGateway(kite().kc, _risk)" in main_src,
)
check(
    "nothing in main.py re-reads the token file on its own",
    "_load_token" not in main_src,
)

for leftover in scratch.iterdir():
    leftover.unlink()
scratch.rmdir()

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("ALL CHECKS PASSED: a request_token handed to /callback updates the live desk in place; "
      "no restart is needed and none is performed.")

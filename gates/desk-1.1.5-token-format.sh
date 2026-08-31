#!/bin/sh
# Gate 1.1.5 G1: report the DEPLOYED desk's on-disk Kite token format, read off the box.
# Prints shape only — never the token. See tools/deploy/desk/README.md.
set -eu
exec ssh -o BatchMode=yes momentum-desk-app 'cd /home/desk/kite-momentum-rebalancer && /usr/bin/python3 - <<PY
import json, os, stat
p = "data/.kite_token.json"
d = json.load(open(p))
print(
    "deployed-token-store format=plain-json keys=%s len=%d bytes=%d mode=%o "
    "fernet_sibling=%s app_token_store_py=%s"
    % (
        ",".join(sorted(d)),
        len(d["access_token"]),
        os.path.getsize(p),
        stat.S_IMODE(os.stat(p).st_mode),
        "present" if os.path.exists(p + ".key") else "absent",
        "present" if os.path.exists("app/token_store.py") else "absent",
    )
)
PY'

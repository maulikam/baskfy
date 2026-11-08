#!/usr/bin/env bash
# Law 1 in CI: baskfy-core's normal dependencies are this list and nothing else.
set -euo pipefail
ALLOWED="chrono polars rust_decimal serde thiserror"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cargo metadata --no-deps --format-version 1 --manifest-path "$ROOT/rust/Cargo.toml" \
  | python3 -c '
import json, sys
allowed = set(sys.argv[1].split())
core = next(p for p in json.load(sys.stdin)["packages"] if p["name"] == "baskfy-core")
extra = sorted({d["name"] for d in core["dependencies"] if d["kind"] is None} - allowed)
if extra:
    sys.exit("baskfy-core gained a dependency outside Law 1: " + ", ".join(extra))
print("baskfy-core dependencies: ok")
' "$ALLOWED"

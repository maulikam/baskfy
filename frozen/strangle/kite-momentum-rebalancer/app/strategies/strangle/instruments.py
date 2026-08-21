"""The underlyings the strangle may be run on, and the state each one keeps to itself.

WHY THIS EXISTS. Every path the runner writes to used to be a module constant, which is
correct for exactly one instrument and silently destructive for two. Three sessions
sharing one journal, one lockout and one straddle record would produce:

  - a lockout earned by SENSEX halting NIFTY, because the lockout is keyed by date only;
  - reference bands built from three different underlyings pooled into one distribution,
    which is not a wide band, it is a meaningless one;
  - a paper record that cannot attribute a single trade to the instrument that made it.

So the slug picks the config, and the config's operational paths are derived from the slug
rather than typed. Adding a fourth underlying is a YAML file, not a code change.

WHAT IS NOT HERE. No lot size, strike step or expiry weekday. Those are market facts, they
change without notice, and config.assert_market_facts checks the YAML against the live
instrument dump at every startup. Duplicating them here would create a second place to be
wrong, and the copy that is never checked is the one that goes stale.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

OUT = "data/outputs"


@dataclass(frozen=True)
class Underlying:
    slug: str
    label: str
    config: str
    # The index whose candles define swing levels. Hardcoding NIFTY's 256265 in the runner
    # was fine while NIFTY was the only instrument; for BANKNIFTY it would have computed
    # support and resistance off the wrong index and never raised.
    index_token: int
    index_key: str
    exchange: str          # options exchange: NFO for NSE, BFO for BSE
    cash_exchange: str     # where the index itself quotes, for the calendar's history call
    series: str            # weekly | monthly — decides how many sessions a cycle offers

    def journal(self) -> str:
        return f"{OUT}/strangle_{self.slug}_journal.jsonl"

    def lockout(self) -> str:
        return f"{OUT}/strangle_{self.slug}_lockout.json"

    def forward(self) -> str:
        return f"{OUT}/strangle_{self.slug}_straddle_record.jsonl"

    def lock(self) -> str:
        return f"{OUT}/strangle_{self.slug}_session.lock"


REGISTRY: dict[str, Underlying] = {
    "nifty": Underlying(
        slug="nifty", label="NIFTY", config="config/strangle.yaml",
        index_token=256265, index_key="NSE:NIFTY 50",
        exchange="NFO", cash_exchange="NSE", series="weekly"),
    "banknifty": Underlying(
        slug="banknifty", label="BANKNIFTY", config="config/strangle_banknifty.yaml",
        index_token=260105, index_key="NSE:NIFTY BANK",
        exchange="NFO", cash_exchange="NSE", series="monthly"),
    "sensex": Underlying(
        slug="sensex", label="SENSEX", config="config/strangle_sensex.yaml",
        index_token=265, index_key="BSE:SENSEX",
        exchange="BFO", cash_exchange="BSE", series="weekly"),
}

DEFAULT = "nifty"


def get(slug: str) -> Underlying:
    key = (slug or DEFAULT).strip().lower()
    if key not in REGISTRY:
        raise KeyError(f"unknown underlying {slug!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[key]


def all_slugs() -> list[str]:
    return list(REGISTRY)


def configured() -> list[Underlying]:
    """Those whose config file is actually present — the UI lists these."""
    return [u for u in REGISTRY.values() if Path(u.config).exists()]

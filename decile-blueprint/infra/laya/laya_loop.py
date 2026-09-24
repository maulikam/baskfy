"""The Laya sidecar: the scan candidates go to Laya for its opinion on their filings.

Runs as the `laya` compose service (`infra/docker/compose.prod.yml`), in its own container with
its own interpreter, so torch never enters the API, worker or desk images and never enters
`uv.lock` (house rule 1 — see `docs/02-tech-stack-adr.md`, "Laya sidecar"). It is deliberately
a script with three dependencies and no import from the product's packages: the product's one
contract with it is the cache key and the JSON shape, both defined in
`baskfy_core.catalyst_tags` and restated here so this file can be read on the box on its own.

WHAT IT DOES, EVERY FIVE MINUTES
--------------------------------
1. Reads the **candidates the scans curated** — every name on the swing, volume-breakout and
   three-weeks-tight tables at each strategy's latest session, the same rows `/build/overlap`
   lists — and, for each, the newest headline the swing feed holds for it (`sw_catalyst`,
   SW11B/A3: a headline, a stamp and a link; never the filing). A candidate with no filing on
   record is not sent: there is nothing to read, and the page says "no filing on record"
   rather than inventing an opinion.
2. For each headline whose key is not in Redis, asks Laya one `choice` question — what kind of
   corporate event the filing announces, over the eight-type vocabulary, with the headline as
   the whole state — and writes the answer under
   `catalyst_tag:v1:<sha256 of the casefolded headline>` with a 30-day TTL.
3. Sleeps.

WHY ONLY THAT QUESTION, FOR NOW
-------------------------------
Measured 25 Sep 2026 on the base checkpoint, zero-shot: the event-type question is right at
0.90 to 0.99 on unambiguous headlines and defers to the rules below 0.60 (`baskfy_core.
catalyst_tags.resolve_tag`). The question a trader actually wants — "does this filing explain
this pattern?" (`supportive_context | adverse_context | probably_unrelated | unclear`) — came
back `probably_unrelated` at 0.21 on a Rs 840 crore defence order under an episodic-pivot gap,
and never above 0.34 on eight rows. That is a question for the fine-tuned checkpoint, trained on
the corrections this column collects; asking it today would print noise with a percentage.

The API (`baskfy_api.overlap._tag`) reads that key and resolves it against the rules baseline;
a miss, an unreachable Redis, or this service being down all mean "the rules tag", never an
error on the page. Nothing here writes to Postgres, and nothing here is read by any rank, size
or order path.

WHY NOT `laya serve`
--------------------
The HTTP server is the right shape when a caller needs an answer now. Nobody does: the feed
writes at 09:17 and the page is read for the rest of the day, so a loop that tags what is new
and caches it is both simpler and cheaper than a request path that would wait about a second
per headline on the box's CPU (measured 25 Sep 2026: fourteen headlines in 17.9 s, batched).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from typing import Any, Protocol

import psycopg
import redis

log = logging.getLogger("laya-loop")

MODEL_ID = os.environ.get("LAYA_MODEL", "convaiinnovations/laya")
INTERVAL_S = int(os.environ.get("LAYA_INTERVAL_SECONDS", "300"))
LOOKBACK_DAYS = int(os.environ.get("LAYA_LOOKBACK_DAYS", "14"))
TTL_S = 30 * 24 * 3600
BATCH = 16

#: Mirrors `baskfy_core.catalyst_tags.LAYA_QUESTIONS`. Keep the two identical.
QUESTIONS: dict[str, dict[str, Any]] = {
    "event_type": {
        "type": "choice",
        "instructions": (
            "What kind of corporate event does the exchange filing `headline` announce?"
        ),
        "criteria": {
            "earnings": "quarterly or annual financial results, or guidance",
            "order": "winning an order, contract, tender or letter of award",
            "approval": "a regulatory approval, licence, certification or patent",
            "fundraising": (
                "raising capital: QIP, preferential issue, rights issue, warrants, debentures"
            ),
            "governance": (
                "a director, auditor or KMP change, a credit rating, or a regulatory or court order"
            ),
            "corporate_action": (
                "dividend, buyback, bonus, split, merger, acquisition, stake, JV, "
                "capacity expansion"
            ),
            "routine": (
                "a routine compliance notice: trading window, certificates, meetings, publications"
            ),
            "other": "the headline does not say what the event is",
        },
    }
}


def cache_key(headline: str) -> str:
    """Mirrors `baskfy_core.catalyst_tags.cache_key`. Keep the two identical."""
    return "catalyst_tag:v1:" + hashlib.sha256(headline.strip().casefold().encode()).hexdigest()


def pg_dsn() -> str:
    """The API's SQLAlchemy URL, as psycopg wants it (`postgresql+asyncpg://` -> `postgresql://`).

    ``BASKFY_DATABASE_URL`` only. This used to prefer ``BASKFY_DATABASE_URL_PG``, a local-dev
    variable that `.env.example` points at ``localhost:5433`` with the dev password; the box's
    `.env.staging` carried that line, so the first pass on 25 Sep 2026 could not connect at all.
    """
    return os.environ["BASKFY_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


#: The candidates, exactly as `baskfy_api.overlap` resolves them: each strategy at its own
#: latest session (the market/breadth row is the detector's clock, C1), joined to the newest
#: headline the feed holds for the name. Names the feed wrote for recently are included too, so a
#: name that was a candidate yesterday and is opened from the instrument page today still has its
#: tag.
#:
#: **The newest headline per name, never every headline (25 Sep 2026).** `recent` used to be
#: every `sw_catalyst` headline created in the lookback, and NSE's announcements read returns a
#: name's whole history (3,351 rows for RELIANCE). Eleven feed names were 4,708 headlines, which
#: held both of the box's CPUs for the whole first pass; the overlap page's scan button would
#: have made it tens of thousands. The page shows one headline per row, so one is what is tagged.
CANDIDATE_HEADLINES_SQL = """
WITH names AS (
    SELECT instrument_id FROM sw_setup_daily
     WHERE date = (SELECT max(date) FROM sw_market_daily)
    UNION
    SELECT instrument_id FROM vb_signal_daily
     WHERE date = (SELECT max(date) FROM vb_breadth_daily)
    UNION
    SELECT instrument_id FROM tw_state_daily
     WHERE date = (SELECT max(date) FROM tw_breadth_daily)
),
newest AS (
    SELECT DISTINCT ON (c.instrument_id) c.instrument_id, c.headline
      FROM sw_catalyst c
      JOIN names n ON n.instrument_id = c.instrument_id
     WHERE c.headline IS NOT NULL AND btrim(c.headline) <> ''
     ORDER BY c.instrument_id, c.published_at DESC NULLS LAST, c.id DESC
),
recent AS (
    SELECT DISTINCT ON (instrument_id) headline FROM sw_catalyst
     WHERE created_at >= now() - make_interval(days => %s)
       AND headline IS NOT NULL AND btrim(headline) <> ''
     ORDER BY instrument_id, published_at DESC NULLS LAST, id DESC
)
SELECT headline FROM newest
UNION
SELECT headline FROM recent
"""


def candidate_headlines(conn: psycopg.Connection[Any]) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(CANDIDATE_HEADLINES_SQL, (LOOKBACK_DAYS,))
        return [str(row[0]) for row in cur.fetchall()]


def untagged(cache: redis.Redis, headlines: list[str]) -> list[str]:
    if not headlines:
        return []
    keys = [cache_key(h) for h in headlines]
    present = cache.mget(keys)
    return [h for h, hit in zip(headlines, present, strict=True) if hit is None]


class Agent(Protocol):
    """The one call this loop makes on a loaded Laya checkpoint."""

    def predict_batch(self, states: list[dict[str, str]], questions: object) -> list[object]: ...


def tag_batch(agent: Agent, cache: redis.Redis, headlines: list[str]) -> int:
    results = agent.predict_batch([{"headline": h} for h in headlines], QUESTIONS)
    written = 0
    for headline, result in zip(headlines, results, strict=True):
        answer = result.get("answers", {}).get("event_type") if isinstance(result, dict) else None
        if not isinstance(answer, dict) or not isinstance(answer.get("choice"), str):
            log.warning("no answer for %r", headline[:80])
            continue
        payload = {
            "choice": answer["choice"],
            "confidence": round(float(answer.get("confidence", 0.0)), 4),
            "probabilities": answer.get("probabilities", {}),
            "model": result.get("model") if isinstance(result, dict) else None,
            "tagged_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        cache.set(cache_key(headline), json.dumps(payload), ex=TTL_S)
        written += 1
    return written


def once(agent: Agent, cache: redis.Redis) -> None:
    with psycopg.connect(pg_dsn(), connect_timeout=10) as conn:
        headlines = candidate_headlines(conn)
    todo = untagged(cache, headlines)
    if not todo:
        log.info("nothing to tag (%d candidate headlines, all cached)", len(headlines))
        return
    started = time.time()
    written = 0
    for i in range(0, len(todo), BATCH):
        written += tag_batch(agent, cache, todo[i : i + BATCH])
    log.info("tagged %d of %d new headlines in %.1fs", written, len(todo), time.time() - started)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import laya  # noqa: PLC0415 - imported late so a missing model fails after logging is up

    log.info("loading %s", MODEL_ID)
    agent = laya.load(MODEL_ID)
    cache = redis.Redis.from_url(os.environ.get("BASKFY_REDIS_URL", "redis://redis:6379/0"))
    while True:
        try:
            once(agent, cache)
        except (psycopg.Error, redis.RedisError) as exc:
            log.error("pass skipped: %s", exc)
        sleep_until_woken(cache)


#: Set by the overlap page's "Scan filings" task when it has written new headlines
#: (`baskfy_api.overlap_scan.LAYA_WAKE_KEY`), so they are tagged now, not at the next pass.
WAKE_KEY = "laya:wake"
WAKE_POLL_S = 5


def sleep_until_woken(cache: redis.Redis) -> None:
    """Sleep ``INTERVAL_S``, or less if the scan button's task asks for a pass sooner."""
    deadline = time.monotonic() + INTERVAL_S
    while time.monotonic() < deadline:
        time.sleep(WAKE_POLL_S)
        try:
            if cache.getdel(WAKE_KEY) is not None:
                log.info("woken by a filings scan")
                return
        except redis.RedisError as exc:
            log.warning("wake check failed: %s", exc)


if __name__ == "__main__":
    sys.exit(main())

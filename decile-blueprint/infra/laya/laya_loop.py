"""The Laya sidecar: the scan candidates go to Laya for its opinion on their filings and rows.

Runs as the `laya` compose service (`infra/docker/compose.prod.yml`), in its own container with
its own interpreter, so torch never enters the API, worker or desk images and never enters
`uv.lock` (house rule 1 — see `docs/02-tech-stack-adr.md`, "Laya sidecar"). It is deliberately
a script with three dependencies and no import from the product's packages: the product's whole
contract with it is two cache keys and one JSON shape, defined in `baskfy_core.catalyst_tags`
and `baskfy_core.candidate_review` and restated here so this file can be read on the box on its
own. `infra/laya/tests/test_laya_loop.py` imports both sides and asserts the mirrors agree.

WHAT IT DOES, EVERY FIVE MINUTES
--------------------------------
1. Reads the **candidates the scans curated** — every name on the swing, volume-breakout and
   three-weeks-tight tables at each strategy's latest session, the same rows `/build/overlap`
   lists — and, for each, the newest headline the swing feed holds for it (`sw_catalyst`,
   SW11B/A3: a headline, a stamp and a link; never the filing), plus the headlines the API has
   noted it is showing and has no answer for (`catalyst_tag:wanted`). A candidate with no
   filing on record is not sent: there is nothing to read.
2. For each headline whose key is not in Redis, asks Laya one `choice` question — what kind of
   corporate event the filing announces, over the eight-type vocabulary, with the headline as
   the whole state — and writes the answer under `catalyst_tag:v2:<schema>:<sha256 of the
   casefolded headline>` with a 30-day TTL.
3. Answers the **row question** on every row state the API queued (`candidate_review:wanted`):
   how much the row deserves a trader's attention — `look_first | worth_a_look | skip` — over
   the filing, when it was, the day's context and the technicals in words. Every field the API
   wrote reaches the model, in `STATE_FIELDS` order.
4. Sleeps.

WHAT THE PAYLOAD'S NUMBER MEANS (OV11, 26 Sep 2026)
---------------------------------------------------
``confidence`` is **the probability Laya put on the answer it chose** — laya's
``answer_confidence``, the quantity its temperature scaling calibrates, so that of the answers
returned at 0.8 about 0.8 are right. From 25 to 26 Sep 2026 both readers stored laya's
``confidence`` field instead, which on a `choice` question is ``1 - H(p)/log(k)``: how
concentrated the whole distribution is, on another scale (three-way 0.60/0.20/0.20 reads 0.14
there). The 0.60 floor was therefore compared to the wrong number. The payload now carries
``answer_confidence`` and, under its own name, ``entropy_confidence``, plus the full
``probabilities`` and the model tag; the key generation moved to v2 so no v1 answer is ever
read against the floor again.

WHY THE MODEL IS PINNED
-----------------------
`laya.load("convaiinnovations/laya")` resolves the repo's *current* revision on every cold
start, and an upstream push would silently change every answer while the cache still held the
old ones. `LAYA_MODEL_REVISION` names the snapshot (the sha the box loaded on 25 Sep 2026 by
default); the loop downloads exactly that revision and hands laya the local directory. On start
it compares the model tag with the one the cache last saw (`laya:model`) and, when it differs,
deletes its own answer keys before writing any new one — the model's revision is part of what an
answer means, without the API having to know it.

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
from collections.abc import Iterator, Mapping, Sequence
from typing import Protocol

import psycopg
import redis

log = logging.getLogger("laya-loop")

MODEL_ID = os.environ.get("LAYA_MODEL", "convaiinnovations/laya")
#: The Hugging Face revision the sidecar loads — the snapshot the box first pulled (25 Sep
#: 2026; the repo's main at 2026-09-24T05:39Z). Move it deliberately, with a re-measurement.
MODEL_REVISION = os.environ.get("LAYA_MODEL_REVISION", "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851")
#: What every answer records as its author, and what a change of resets the cache.
MODEL_TAG = f"{MODEL_ID}@{MODEL_REVISION[:12]}"
INTERVAL_S = int(os.environ.get("LAYA_INTERVAL_SECONDS", "300"))
LOOKBACK_DAYS = int(os.environ.get("LAYA_LOOKBACK_DAYS", "14"))
TTL_S = 30 * 24 * 3600
BATCH = 16

#: Mirrors `baskfy_core.catalyst_tags.LAYA_QUESTIONS`. Keep the two identical.
QUESTIONS: dict[str, dict[str, object]] = {
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

#: Mirrors `baskfy_core.catalyst_tags.CACHE_KEY_VERSION`. Keep the two identical.
CACHE_KEY_VERSION = "v2"


def question_schema_hash(questions: Mapping[str, object]) -> str:
    """Mirrors `baskfy_core.catalyst_tags.question_schema_hash`. Keep the two identical."""
    canonical = json.dumps(questions, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


TAG_SCHEMA = question_schema_hash(QUESTIONS)


def cache_key(headline: str) -> str:
    """Mirrors `baskfy_core.catalyst_tags.cache_key`. Keep the two identical."""
    digest = hashlib.sha256(headline.strip().casefold().encode()).hexdigest()
    return f"catalyst_tag:{CACHE_KEY_VERSION}:{TAG_SCHEMA}:{digest}"


def pg_dsn() -> str:
    """The API's SQLAlchemy URL, as psycopg wants it (`postgresql+asyncpg://` -> `postgresql://`).

    ``BASKFY_DATABASE_URL`` only. This used to prefer ``BASKFY_DATABASE_URL_PG``, a local-dev
    variable that `.env.example` points at ``localhost:5433`` with the dev password; the box's
    `.env.staging` carried that line, so the first pass on 25 Sep 2026 could not connect at all.
    """
    return os.environ["BASKFY_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


class Cache(Protocol):
    """The nine Redis calls this loop makes, so a test can stand in an in-memory one and mypy
    can check both against the same surface. `redis.Redis` satisfies it structurally."""

    def get(self, name: str) -> object: ...
    def set(self, name: str, value: str, ex: int | None = None) -> object: ...
    def mget(self, keys: Sequence[str]) -> object: ...
    def hgetall(self, name: str) -> object: ...
    def hdel(self, name: str, *keys: str) -> object: ...
    def smembers(self, name: str) -> object: ...
    def srem(self, name: str, *values: str) -> object: ...
    def getdel(self, name: str) -> object: ...
    def scan_iter(self, match: str) -> Iterator[object]: ...
    def delete(self, *names: str) -> object: ...


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


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


def candidate_headlines(conn: psycopg.Connection[tuple[object, ...]]) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(CANDIDATE_HEADLINES_SQL, (LOOKBACK_DAYS,))
        return [str(row[0]) for row in cur.fetchall()]


def untagged(cache: Cache, headlines: list[str]) -> list[str]:
    if not headlines:
        return []
    keys = [cache_key(h) for h in headlines]
    raw = cache.mget(keys)
    present: list[object] = list(raw) if isinstance(raw, list) else [None] * len(keys)
    return [h for h, hit in zip(headlines, present, strict=True) if hit is None]


class Agent(Protocol):
    """The one call this loop makes on a loaded Laya checkpoint."""

    def predict_batch(self, states: list[dict[str, str]], questions: object) -> list[object]: ...


def _answer(result: object, question: str) -> dict[str, object] | None:
    """The typed answer to one question out of one `predict_batch` result, or ``None``."""
    if not isinstance(result, dict):
        return None
    answers = result.get("answers")
    if not isinstance(answers, dict):
        return None
    answer = answers.get(question)
    if not isinstance(answer, dict) or not isinstance(answer.get("choice"), str):
        return None
    return {str(k): v for k, v in answer.items()}


def _number(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def payload(answer: Mapping[str, object], *, model: str = MODEL_TAG) -> str | None:
    """The JSON the cache holds for one answer, or ``None`` when laya gave no probability.

    ``confidence`` is `answer_confidence` (see the module docstring), else the chosen answer's
    entry in ``probabilities``; laya's entropy score is kept as ``entropy_confidence`` so it is
    on record and never compared to the floor. `baskfy_core.catalyst_tags.chosen_probability`
    is the reader.
    """
    choice = answer.get("choice")
    if not isinstance(choice, str):
        return None
    probabilities = answer.get("probabilities")
    probs = (
        {str(k): v for k, v in probabilities.items() if _number(v) is not None}
        if isinstance(probabilities, dict)
        else {}
    )
    chosen = _number(answer.get("answer_confidence"))
    if chosen is None:
        chosen = _number(probs.get(choice))
    if chosen is None:
        return None
    entropy = _number(answer.get("confidence"))
    return json.dumps(
        {
            "choice": choice,
            "confidence": round(chosen, 4),
            "answer_confidence": round(chosen, 4),
            "entropy_confidence": None if entropy is None else round(entropy, 4),
            "probabilities": {k: round(float(_number(v) or 0.0), 4) for k, v in probs.items()},
            "model": model,
            "tagged_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
    )


def tag_batch(agent: Agent, cache: Cache, headlines: list[str]) -> int:
    results = agent.predict_batch([{"headline": h} for h in headlines], QUESTIONS)
    written = 0
    for headline, result in zip(headlines, results, strict=True):
        answer = _answer(result, "event_type")
        body = None if answer is None else payload(answer)
        if body is None:
            log.warning("no answer for %r", headline[:80])
            continue
        cache.set(cache_key(headline), body, ex=TTL_S)
        written += 1
    return written


#: The headlines the overlap page is showing and does not have a tag for yet, written by the
#: API on every read (`baskfy_api.overlap.WANTED_KEY`). Since the page shows the *material*
#: filing per name rather than the newest (25 Sep 2026), the newest-per-name query above is not
#: enough on its own: the rules may pick a result or an order win from last week, and that is
#: the headline Laya's opinion is wanted on. Members are dropped once tagged.
WANTED_KEY = "catalyst_tag:wanted"


def wanted_headlines(cache: Cache) -> list[str]:
    raw = cache.smembers(WANTED_KEY)
    members = raw if isinstance(raw, set | list | tuple) else ()
    return sorted(_text(member) for member in members if member)


#: Written after every pass so the page can say when Laya last looked and how much it tagged
#: (`baskfy_api.overlap` reads it as `laya.last_pass_at`). "Laya has not run" and "Laya ran and
#: was unsure" are different answers to "is Laya working", and only this key tells them apart.
HEARTBEAT_KEY = "catalyst_tag:heartbeat"
HEARTBEAT_TTL_S = 2 * 24 * 3600


def heartbeat(cache: Cache, *, candidates: int, tagged: int, seconds: float) -> None:
    cache.set(
        HEARTBEAT_KEY,
        json.dumps(
            {
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "model": MODEL_TAG,
                "candidates": candidates,
                "tagged": tagged,
                "seconds": round(seconds, 1),
            }
        ),
        ex=HEARTBEAT_TTL_S,
    )


#: Mirrors `baskfy_core.candidate_review.REVIEW_QUESTIONS`. Keep the two identical. The row
#: states themselves come from the API (`baskfy_api.overlap.REVIEW_WANTED_KEY`, a hash of
#: key -> state JSON) — only the API holds the rows, and only
#: `baskfy_core.candidate_review.review_state` may say the numbers in words.
REVIEW_WANTED_KEY = "candidate_review:wanted"
REVIEW_QUESTIONS: dict[str, dict[str, object]] = {
    "review_priority": {
        "type": "choice",
        "instructions": (
            "`setup` describes a technical pattern a screener found on a stock, in words. "
            "`filing` is the material exchange filing chosen for it. `timeline` says when the "
            "filing, the results and the screen ranks were, relative to the setup's session. "
            "`context` is that day's market gates and breadth, the sector, and the screens the "
            "stock ranks on. How much does this row deserve a trader's attention before the "
            "others on the same list?"
        ),
        "criteria": {
            "look_first": (
                "the pattern is strong and the filing is the kind of news that produces it "
                "(an order win, a result, an approval, an expansion)"
            ),
            "worth_a_look": (
                "the pattern is real but the filing is routine or unknown, or the filing is "
                "good but the pattern is weak"
            ),
            "skip": (
                "the pattern is weak or stale, or the filing is adverse (a regulatory order, "
                "a default, a resignation under a cloud)"
            ),
        },
    }
}

#: Mirrors `baskfy_core.candidate_review.STATE_FIELDS`. Keep the two identical. The order the
#: model sees the fields in: laya serialises a dict in insertion order and truncates from the
#: right, so the short fields lead and the long `setup` closes — what a two-strategy row loses
#: to the window is the tail of its technicals, never the filing or its date. The API writes the
#: hash with sorted keys; this puts them back. From 25 to 26 Sep 2026 this function rebuilt only
#: `setup` and `filing`, so the context the API composed never reached the model (OV11).
STATE_FIELDS = ("filing", "timeline", "context", "setup")


def review_state(queued: object) -> dict[str, str] | None:
    """The queued state as the model sees it: every string field, in `STATE_FIELDS` order, then
    any field this file does not know yet (so a new field from the API is not dropped again).
    ``None`` when the entry is not a state with a `setup`."""
    if not isinstance(queued, dict) or not isinstance(queued.get("setup"), str):
        return None
    strings = {str(k): v for k, v in queued.items() if isinstance(v, str) and v}
    ordered = {name: strings[name] for name in STATE_FIELDS if name in strings}
    ordered.update({k: v for k, v in strings.items() if k not in ordered})
    return ordered


def review_rows(agent: Agent, cache: Cache) -> int:
    """Answer the attention question on every row state the API queued, and clear the queue.

    The answer is cached under the row's content-addressed key for thirty days; the API shows
    it only above its confidence floor, so an unsure answer costs nothing on the page and is
    still there for a person to see when labelling the row.
    """
    raw = cache.hgetall(REVIEW_WANTED_KEY)
    queued = raw if isinstance(raw, dict) else {}
    if not queued:
        return 0
    keys: list[str] = []
    states: list[dict[str, str]] = []
    for raw_key, raw_state in queued.items():
        key = _text(raw_key)
        try:
            state = review_state(json.loads(raw_state))
        except ValueError:
            state = None
        if state is None:
            cache.hdel(REVIEW_WANTED_KEY, key)
            continue
        keys.append(key)
        states.append(state)
    written = 0
    for i in range(0, len(states), BATCH):
        batch_keys, batch_states = keys[i : i + BATCH], states[i : i + BATCH]
        results = agent.predict_batch(batch_states, REVIEW_QUESTIONS)
        for key, result in zip(batch_keys, results, strict=True):
            answer = _answer(result, "review_priority")
            body = None if answer is None else payload(answer)
            if body is None:
                log.warning("no opinion for row %s", key[-12:])
                continue
            cache.set(key, body, ex=TTL_S)
            written += 1
        cache.hdel(REVIEW_WANTED_KEY, *batch_keys)
    return written


#: The model tag the cache's answers were written by. A different tag on start means every
#: cached answer is another model's, and they go before the first new one is written.
MODEL_KEY = "laya:model"
ANSWER_PATTERNS = (f"catalyst_tag:{CACHE_KEY_VERSION}:*", f"candidate_review:{CACHE_KEY_VERSION}:*")


def reset_on_model_change(cache: Cache, model: str = MODEL_TAG) -> int:
    """Delete this sidecar's answer keys when the loaded model is not the one that wrote them;
    record the model either way. Returns how many keys went. The `wanted` sets, the heartbeat
    and the wake flag are not answers and stay."""
    seen = cache.get(MODEL_KEY)
    if seen is not None and _text(seen) == model:
        return 0
    gone = 0
    for pattern in ANSWER_PATTERNS:
        batch = [_text(key) for key in cache.scan_iter(match=pattern)]
        for i in range(0, len(batch), 500):
            cache.delete(*batch[i : i + 500])
        gone += len(batch)
    cache.set(MODEL_KEY, model)
    if seen is not None:
        log.info("model changed from %s to %s: dropped %d cached answers", _text(seen), model, gone)
    return gone


def once(agent: Agent, cache: Cache) -> None:
    with psycopg.connect(pg_dsn(), connect_timeout=10) as conn:
        headlines = candidate_headlines(conn)
    wanted = wanted_headlines(cache)
    headlines = sorted(set(headlines) | set(wanted))
    todo = untagged(cache, headlines)
    if wanted:
        # Whatever is cached now is no longer wanted; what this pass tags is dropped below.
        already = [h for h in wanted if h not in todo]
        if already:
            cache.srem(WANTED_KEY, *already)
    reviewed = review_rows(agent, cache)
    if reviewed:
        log.info("answered the attention question on %d rows", reviewed)
    if not todo:
        log.info("nothing to tag (%d candidate headlines, all cached)", len(headlines))
        heartbeat(cache, candidates=len(headlines), tagged=reviewed, seconds=0.0)
        return
    started = time.time()
    written = 0
    for i in range(0, len(todo), BATCH):
        written += tag_batch(agent, cache, todo[i : i + BATCH])
    if wanted:
        cache.srem(WANTED_KEY, *[h for h in wanted if h in todo])
    log.info("tagged %d of %d new headlines in %.1fs", written, len(todo), time.time() - started)
    heartbeat(cache, candidates=len(headlines), tagged=written, seconds=time.time() - started)


#: The files one checkpoint needs — the same four patterns `laya.Agent` would ask the hub for,
#: restricted to the repo root so the sibling checkpoints (multilingual, typed-decisions) are
#: not downloaded.
SNAPSHOT_FILES = ("rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*")


def snapshot_dir(model: str = MODEL_ID, revision: str = MODEL_REVISION) -> str:
    """The local directory of exactly this revision of the checkpoint, fetched if absent. laya
    is then handed the path, so it never resolves the repo's moving head itself."""
    from huggingface_hub import snapshot_download  # noqa: PLC0415 - sidecar-only dependency

    return str(snapshot_download(model, revision=revision, allow_patterns=list(SNAPSHOT_FILES)))


def load_agent() -> Agent:
    import laya  # noqa: PLC0415 - imported late so a missing model fails after logging is up

    log.info("loading %s", MODEL_TAG)
    agent: Agent = laya.load(snapshot_dir())
    return agent


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    agent = load_agent()
    cache = redis.Redis.from_url(os.environ.get("BASKFY_REDIS_URL", "redis://redis:6379/0"))
    reset_on_model_change(cache)
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


def sleep_until_woken(cache: Cache) -> None:
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

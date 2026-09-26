"""The sidecar's contract with the product: the keys, the payload, and what the model is shown.

The loop is a script outside the packages (house rule 1 keeps torch out of the stack), so it is
imported here by path and exercised with a fake agent and an in-memory cache — no model, no
Redis, no Postgres. The mirrors it keeps of `baskfy_core` (`catalyst_tags`, `candidate_review`)
are asserted equal to the originals, which is the only way "keep the two identical" is enforced.

Written for OV11 (26 Sep 2026), after a review found three faults the sidecar had no test to
catch: it cached laya's entropy score as the probability, it rebuilt the row state from two of
its fields and dropped the rest, and nothing pinned the checkpoint it loaded.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import ModuleType

import pytest

from baskfy_core import candidate_review, catalyst_tags

LOOP = Path(__file__).resolve().parents[1] / "laya_loop.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("laya_loop", LOOP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


loop = _load()


class FakeCache:
    """The nine calls the loop makes, over a dict. Values are bytes, as redis-py returns them."""

    def __init__(self) -> None:
        self.kv: dict[str, bytes] = {}
        self.hashes: dict[str, dict[str, bytes]] = {}
        self.sets: dict[str, set[str]] = {}
        self.ttl: dict[str, int | None] = {}

    def get(self, name: str) -> object:
        return self.kv.get(name)

    def set(self, name: str, value: str, ex: int | None = None) -> object:
        self.kv[name] = value.encode()
        self.ttl[name] = ex
        return True

    def mget(self, keys: Sequence[str]) -> object:
        return [self.kv.get(k) for k in keys]

    def hgetall(self, name: str) -> object:
        return {k.encode(): v for k, v in self.hashes.get(name, {}).items()}

    def hdel(self, name: str, *keys: str) -> object:
        bucket = self.hashes.get(name, {})
        return sum(1 for k in keys if bucket.pop(k, None) is not None)

    def smembers(self, name: str) -> object:
        return {m.encode() for m in self.sets.get(name, set())}

    def srem(self, name: str, *values: str) -> object:
        bucket = self.sets.get(name, set())
        before = len(bucket)
        bucket.difference_update(values)
        return before - len(bucket)

    def getdel(self, name: str) -> object:
        return self.kv.pop(name, None)

    def scan_iter(self, match: str) -> Iterator[object]:
        return (k.encode() for k in list(self.kv) if fnmatch.fnmatchcase(k, match))

    def delete(self, *names: str) -> object:
        return sum(1 for n in names if self.kv.pop(n, None) is not None)


class FakeAgent:
    """Answers in the shape laya 0.3.20's `predict_batch` returns, and remembers what it saw."""

    def __init__(self, answers: list[dict[str, object]]) -> None:
        self.answers = answers
        self.seen: list[tuple[list[dict[str, str]], object]] = []

    def predict_batch(self, states: list[dict[str, str]], questions: object) -> list[object]:
        self.seen.append((states, questions))
        [question] = list(questions) if isinstance(questions, dict) else ["?"]
        return [
            {"model": "laya-rl-agent", "answers": {question: answer}}
            for answer in self.answers[: len(states)]
        ]


def _laya_answer(
    choice: str, probabilities: dict[str, float], *, entropy: float, calibrated: float | None
) -> dict[str, object]:
    answer: dict[str, object] = {
        "type": "choice",
        "choice": choice,
        "probabilities": probabilities,
        "confidence": entropy,
        "action": {"act_probability": 0.5},
    }
    if calibrated is not None:
        answer["answer_confidence"] = calibrated
    return answer


class TestTheMirrors:
    """What this file restates from `baskfy_core` must be the same text and the same key."""

    def test_the_two_question_dicts_are_identical(self) -> None:
        assert loop.QUESTIONS == catalyst_tags.LAYA_QUESTIONS
        assert loop.REVIEW_QUESTIONS == candidate_review.REVIEW_QUESTIONS

    def test_the_key_version_and_schema_hash_and_key_agree(self) -> None:
        assert loop.CACHE_KEY_VERSION == catalyst_tags.CACHE_KEY_VERSION == "v2"
        assert loop.TAG_SCHEMA == catalyst_tags.TAG_SCHEMA
        assert loop.question_schema_hash(loop.REVIEW_QUESTIONS) == candidate_review.REVIEW_SCHEMA
        headline = "  Receipt of ORDER worth Rs 840 crore "
        assert loop.cache_key(headline) == catalyst_tags.cache_key(headline)
        assert loop.cache_key(headline).startswith(f"catalyst_tag:v2:{catalyst_tags.TAG_SCHEMA}:")

    def test_the_state_field_order_and_the_queue_names_agree(self) -> None:
        assert loop.STATE_FIELDS == candidate_review.STATE_FIELDS
        assert loop.STATE_FIELDS[0] == "filing" and loop.STATE_FIELDS[-1] == "setup"

    def test_a_changed_question_is_a_new_key(self) -> None:
        changed = json.loads(json.dumps(loop.QUESTIONS))
        changed["event_type"]["instructions"] += " Be brief."
        assert loop.question_schema_hash(changed) != loop.TAG_SCHEMA


class TestThePayload:
    """`confidence` is the probability of the chosen word; the entropy score keeps its own name."""

    def test_the_calibrated_number_is_stored_as_confidence_and_the_entropy_beside_it(self) -> None:
        answer = _laya_answer(
            "order", {"order": 0.9861, "routine": 0.01}, entropy=0.9312, calibrated=0.9861
        )
        body = loop.payload(answer, model="convaiinnovations/laya@55cf4c4ebb4e")
        assert body is not None
        stored = json.loads(body)
        assert stored["confidence"] == 0.9861
        assert stored["answer_confidence"] == 0.9861
        assert stored["entropy_confidence"] == 0.9312
        assert stored["probabilities"] == {"order": 0.9861, "routine": 0.01}
        assert stored["model"] == "convaiinnovations/laya@55cf4c4ebb4e"
        assert stored["choice"] == "order"

    def test_without_answer_confidence_the_chosen_probability_stands_in(self) -> None:
        # A three-way answer at p = 0.60/0.20/0.20 has an entropy score of about 0.14. The
        # floor is 0.60: the old field would have hidden a shown-worthy answer.
        answer = _laya_answer(
            "look_first",
            {"look_first": 0.6, "worth_a_look": 0.2, "skip": 0.2},
            entropy=0.1383,
            calibrated=None,
        )
        body = loop.payload(answer)
        assert body is not None
        assert json.loads(body)["confidence"] == 0.6

    def test_an_answer_with_no_probability_at_all_is_not_cached(self) -> None:
        assert loop.payload({"choice": "order", "confidence": 0.93}) is None
        assert loop.payload({"confidence": 0.93}) is None

    def test_the_product_s_readers_accept_what_the_sidecar_writes(self) -> None:
        body = loop.payload(
            _laya_answer("skip", {"skip": 0.71, "look_first": 0.2}, entropy=0.4, calibrated=0.71)
        )
        assert body is not None
        opinion = candidate_review.opinion_from_laya(json.loads(body))
        assert opinion is not None and (opinion.label, opinion.confidence) == ("skip", 0.71)
        tag_body = loop.payload(
            _laya_answer("order", {"order": 0.98}, entropy=0.95, calibrated=0.98)
        )
        assert tag_body is not None
        tag = catalyst_tags.tag_from_laya(json.loads(tag_body))
        assert tag is not None and (tag.event_type, tag.confidence) == ("order", 0.98)


class TestTheHeadlinePass:
    def test_untagged_headlines_are_asked_and_cached_under_the_v2_key(self) -> None:
        cache = FakeCache()
        agent = FakeAgent(
            [
                _laya_answer("order", {"order": 0.98}, entropy=0.9, calibrated=0.98),
                _laya_answer("routine", {"routine": 0.55}, entropy=0.2, calibrated=0.55),
            ]
        )
        headlines = ["Receipt of order", "Closure of trading window"]
        assert loop.untagged(cache, headlines) == headlines
        assert loop.tag_batch(agent, cache, headlines) == 2
        [(states, questions)] = agent.seen
        assert states == [{"headline": h} for h in headlines]
        assert questions is loop.QUESTIONS
        assert loop.untagged(cache, headlines) == []
        stored = json.loads(cache.kv[loop.cache_key("Receipt of order")])
        assert (stored["choice"], stored["confidence"]) == ("order", 0.98)
        assert cache.ttl[loop.cache_key("Receipt of order")] == loop.TTL_S

    def test_a_result_without_an_answer_is_skipped_not_cached(self) -> None:
        cache = FakeCache()
        agent = FakeAgent([{"type": "choice", "confidence": 0.9}])
        assert loop.tag_batch(agent, cache, ["Receipt of order"]) == 0
        assert cache.kv == {}


class TestTheRowPass:
    """Every field the API queued reaches the model, short fields first (OV11 finding 2)."""

    @staticmethod
    def _queue(cache: FakeCache, key: str, state: dict[str, str]) -> None:
        # The API writes the state with sorted keys (`baskfy_api.overlap.review_opinions`).
        cache.hashes.setdefault(loop.REVIEW_WANTED_KEY, {})[key] = json.dumps(
            state, sort_keys=True, ensure_ascii=False
        ).encode()

    def test_every_string_field_reaches_the_model_in_state_field_order(self) -> None:
        cache = FakeCache()
        state = {
            "setup": "Swing episodic pivot, gap day: gapped 9%.",
            "filing": "Receipt of order worth Rs 840 crore",
            "context": "Swing gate green; sector defence; on screens RSI Scan #12 of 400.",
            "timeline": "Swing facts are from the session of Thu 25 Sep 2026; the filing was "
            "published 2 days before the session (Tue 23 Sep 2026).",
        }
        key = candidate_review.review_key(state)
        self._queue(cache, key, state)
        agent = FakeAgent(
            [
                _laya_answer(
                    "look_first",
                    {"look_first": 0.66, "worth_a_look": 0.24, "skip": 0.1},
                    entropy=0.3,
                    calibrated=0.66,
                )
            ]
        )
        assert loop.review_rows(agent, cache) == 1
        [(states, questions)] = agent.seen
        assert questions is loop.REVIEW_QUESTIONS
        [seen] = states
        assert seen == state
        assert list(seen) == ["filing", "timeline", "context", "setup"]
        stored = json.loads(cache.kv[key])
        assert (stored["choice"], stored["confidence"]) == ("look_first", 0.66)
        assert cache.hashes[loop.REVIEW_WANTED_KEY] == {}

    def test_a_field_this_file_does_not_know_is_passed_through_after_the_known_ones(self) -> None:
        state = {"setup": "s", "filing": "f", "later_field": "x"}
        assert loop.review_state(state) == {"filing": "f", "setup": "s", "later_field": "x"}
        assert list(loop.review_state(state) or {}) == ["filing", "setup", "later_field"]

    def test_an_entry_that_is_not_a_state_is_dropped_from_the_queue(self) -> None:
        cache = FakeCache()
        cache.hashes[loop.REVIEW_WANTED_KEY] = {
            "candidate_review:v2:x:bad-json": b"{not json",
            "candidate_review:v2:x:no-setup": json.dumps({"filing": "f"}).encode(),
        }
        agent = FakeAgent([])
        assert loop.review_rows(agent, cache) == 0
        assert agent.seen == []
        assert cache.hashes[loop.REVIEW_WANTED_KEY] == {}

    def test_an_unsure_answer_is_still_cached_so_a_person_can_see_it(self) -> None:
        cache = FakeCache()
        state = {"setup": "Swing flag, still setting up below the pivot.", "filing": "Board meet"}
        key = candidate_review.review_key(state)
        self._queue(cache, key, state)
        agent = FakeAgent(
            [
                _laya_answer(
                    "worth_a_look",
                    {"look_first": 0.32, "worth_a_look": 0.38, "skip": 0.30},
                    entropy=0.01,
                    calibrated=0.38,
                )
            ]
        )
        assert loop.review_rows(agent, cache) == 1
        stored = json.loads(cache.kv[key])
        assert stored["confidence"] == 0.38 < candidate_review.REVIEW_CONFIDENCE_FLOOR
        assert stored["entropy_confidence"] == 0.01


class TestTheModelPin:
    def test_the_revision_is_pinned_and_named_in_the_tag(self) -> None:
        assert len(loop.MODEL_REVISION) == 40
        assert f"{loop.MODEL_ID}@{loop.MODEL_REVISION[:12]}" == loop.MODEL_TAG
        assert loop.SNAPSHOT_FILES == (
            "rl_agent_config.json",
            "model.safetensors",
            "tokenizer/*",
            "encoder/*",
        )

    def test_the_snapshot_is_fetched_at_exactly_that_revision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str, list[str]]] = []

        def fake_download(model: str, *, revision: str, allow_patterns: list[str]) -> str:
            calls.append((model, revision, allow_patterns))
            return "/models/hf/snapshots/" + revision

        hub = ModuleType("huggingface_hub")
        setattr(hub, "snapshot_download", fake_download)  # noqa: B010 - a stand-in module
        monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
        assert loop.snapshot_dir("convaiinnovations/laya", "abc123") == (
            "/models/hf/snapshots/abc123"
        )
        assert calls == [("convaiinnovations/laya", "abc123", list(loop.SNAPSHOT_FILES))]

    def test_a_model_change_drops_the_answers_and_only_the_answers(self) -> None:
        cache = FakeCache()

        def seed() -> None:
            cache.set(loop.cache_key("Receipt of order"), "{}", ex=10)
            cache.set("candidate_review:v2:abcd1234:deadbeef", "{}", ex=10)
            cache.set("candidate_review:v1:oldkey", "{}", ex=10)  # a v1 leftover, never read
            cache.set(loop.HEARTBEAT_KEY, "{}", ex=10)
            cache.sets[loop.WANTED_KEY] = {"Receipt of order"}
            cache.hashes[loop.REVIEW_WANTED_KEY] = {"k": b"{}"}

        def untouched() -> None:
            assert "candidate_review:v1:oldkey" in cache.kv
            assert loop.HEARTBEAT_KEY in cache.kv
            assert cache.sets[loop.WANTED_KEY] == {"Receipt of order"}
            assert cache.hashes[loop.REVIEW_WANTED_KEY] == {"k": b"{}"}

        # First start, nothing recorded: answers of unknown authorship go, and the model is
        # recorded. The queues, the heartbeat and a v1 leftover are not answers and stay.
        seed()
        assert loop.reset_on_model_change(cache, "convaiinnovations/laya@aaaaaaaaaaaa") == 2
        assert loop.cache_key("Receipt of order") not in cache.kv
        untouched()
        assert cache.kv[loop.MODEL_KEY] == b"convaiinnovations/laya@aaaaaaaaaaaa"
        # The same model again: everything it wrote is kept.
        seed()
        assert loop.reset_on_model_change(cache, "convaiinnovations/laya@aaaaaaaaaaaa") == 0
        assert loop.cache_key("Receipt of order") in cache.kv
        # A new revision: its predecessor's answers go.
        assert loop.reset_on_model_change(cache, "convaiinnovations/laya@bbbbbbbbbbbb") == 2
        assert loop.cache_key("Receipt of order") not in cache.kv
        assert "candidate_review:v2:abcd1234:deadbeef" not in cache.kv
        untouched()
        assert cache.kv[loop.MODEL_KEY] == b"convaiinnovations/laya@bbbbbbbbbbbb"


class TestTheHeartbeatAndWanted:
    def test_wanted_headlines_are_decoded_and_sorted(self) -> None:
        cache = FakeCache()
        cache.sets[loop.WANTED_KEY] = {"b headline", "a headline", ""}
        assert loop.wanted_headlines(cache) == ["a headline", "b headline"]

    def test_the_heartbeat_names_the_pinned_model(self) -> None:
        cache = FakeCache()
        loop.heartbeat(cache, candidates=3, tagged=2, seconds=1.234)
        beat = json.loads(cache.kv[loop.HEARTBEAT_KEY])
        assert (beat["model"], beat["candidates"], beat["tagged"], beat["seconds"]) == (
            loop.MODEL_TAG,
            3,
            2,
            1.2,
        )
        assert cache.ttl[loop.HEARTBEAT_KEY] == loop.HEARTBEAT_TTL_S

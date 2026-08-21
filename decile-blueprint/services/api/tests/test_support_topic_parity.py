"""The web form's subject list matches the API's (Prompt 18 §2).

`SupportMessageIn.topic` is a closed `Literal` because the value reaches an email subject line,
and a subject line is a header. No ``/meta/*`` endpoint publishes the set, so
``apps/web/src/lib/marketing/support-topics.ts`` carries its own copy and this test is the link —
the same arrangement as :mod:`packages.core.tests.test_operand_parity`.

Without it, the form could offer a subject the API answers 400 for, and the only way anyone would
find out is a visitor whose support message vanished.
"""

from __future__ import annotations

import re
from pathlib import Path

from baskfy_api.schemas import SUPPORT_TOPICS

REPO_ROOT = Path(__file__).resolve().parents[3]
TOPICS_TS = REPO_ROOT / "apps" / "web" / "src" / "lib" / "marketing" / "support-topics.ts"

_ARRAY = re.compile(r"SUPPORT_TOPICS\s*=\s*\[(.*?)\]\s*as const", re.DOTALL)
_ENTRY = re.compile(r'"([^"]+)"')


def typescript_topics() -> tuple[str, ...]:
    source = TOPICS_TS.read_text(encoding="utf-8")
    match = _ARRAY.search(source)
    assert match is not None, f"SUPPORT_TOPICS not found in {TOPICS_TS}"
    return tuple(_ENTRY.findall(match.group(1)))


def test_the_typescript_file_exists() -> None:
    assert TOPICS_TS.is_file(), f"{TOPICS_TS} is missing"


def test_the_lists_are_identical_and_in_the_same_order() -> None:
    """Same order too: the first entry is the form's default, and it should be the common case."""
    assert typescript_topics() == SUPPORT_TOPICS


def test_no_topic_contains_a_header_delimiter() -> None:
    """Belt and braces: a CR or LF in one of these would be an injection into the subject line."""
    assert all("\r" not in topic and "\n" not in topic for topic in SUPPORT_TOPICS)

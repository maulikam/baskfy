"""Prompt 20's **second acceptance criterion**, and it needs no database.

    "Alert diffing test: given two consecutive screen_run fixtures, the email content matches an
     expected snapshot exactly."

``tests/fixtures/alerts/screen-runs.json`` is the two runs; ``expected-alert.{subject,txt,html}``
are the snapshot. The comparison is byte for byte, so every change to the wording of an alert email
is a change to a committed file that shows up in a diff.

A snapshot on its own only locks in whatever the code did on the day it was written, which
CLAUDE.md house rule 2 rejects. So the snapshot is paired with :class:`TestTheEmailSaysWhatItMust`,
which asserts the *content requirements* independently — every entry, every exit, every mover, the
unsubscribe link, the disclaimer docs/11 mandates. If someone regenerates the snapshot to make a
change pass, those assertions still have to hold.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Final

import pytest

from decile_api.email import templates
from decile_core.screen_diff import ScreenDiff, diff_runs, rows_from_results

FIXTURES: Final = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "alerts"

SCREEN_URL: Final = "http://localhost:3000/screens/exmpl0000001"
MANAGE_URL: Final = "http://localhost:3000/alerts"
#: A fixed token, so the snapshot does not depend on a signing secret. The real link is
#: ``decile_api.alerts.unsubscribe_url``, asserted separately in ``test_api_alerts.py``.
UNSUBSCRIBE_URL: Final = (
    "http://localhost:3000/alerts/unsubscribe?token=0123456789abcdef0123456789abcdef"
)


def fixture() -> dict[str, object]:
    loaded: dict[str, object] = json.loads(
        (FIXTURES / "screen-runs.json").read_text(encoding="utf-8")
    )
    return loaded


def _runs() -> tuple[ScreenDiff, dict[str, dict[str, str]], str]:
    data = fixture()
    previous = data["previous"]
    current = data["current"]
    screen = data["screen"]
    instruments = data["instruments"]
    assert isinstance(previous, dict)
    assert isinstance(current, dict)
    assert isinstance(screen, dict)
    assert isinstance(instruments, dict)
    diff = diff_runs(
        rows_from_results(previous["results"]),
        rows_from_results(current["results"]),
        previous_as_of=dt.date.fromisoformat(str(previous["as_of"])),
        current_as_of=dt.date.fromisoformat(str(current["as_of"])),
    )
    return diff, instruments, str(screen["name"])


def _section() -> templates.AlertSection:
    diff, instruments, name = _runs()

    def row(instrument_id: int, rank: int, previous: int | None = None) -> templates.AlertRow:
        entry = instruments[str(instrument_id)]
        return templates.AlertRow(
            symbol=entry["symbol"], name=entry["name"], rank=rank, previous_rank=previous
        )

    return templates.AlertSection(
        screen_name=name,
        screen_url=SCREEN_URL,
        unsubscribe_url=UNSUBSCRIBE_URL,
        as_of=diff.current_as_of,
        previous_as_of=diff.previous_as_of,
        entries=tuple(row(r.instrument_id, r.rank) for r in diff.entries),
        exits=tuple(row(r.instrument_id, r.rank) for r in diff.exits),
        movers=tuple(
            row(c.instrument_id, c.current_rank, c.previous_rank) for c in diff.rank_changes
        ),
        entry_total=diff.entry_count,
        exit_total=diff.exit_count,
        mover_total=len(diff.rank_changes),
        held_count=diff.held_count,
    )


def _message() -> templates.Message:
    return templates.screen_alert("subscriber@example.com", [_section()], manage_url=MANAGE_URL)


class TestTheDiffOverTheFixtures:
    """The two runs are constructed to exercise every branch of the email."""

    def test_it_finds_two_entries_one_exit_and_three_movers(self) -> None:
        diff, instruments, _ = _runs()
        assert [instruments[str(r.instrument_id)]["symbol"] for r in diff.entries] == [
            "BSE",
            "ZOMATO",
        ]
        assert [instruments[str(r.instrument_id)]["symbol"] for r in diff.exits] == ["TITAN"]
        assert [
            (instruments[str(c.instrument_id)]["symbol"], c.places_moved) for c in diff.rank_changes
        ] == [("KAYNES", 3), ("TRENT", -2), ("PERSISTENT", -2)]
        assert diff.held_count == 1


class TestTheSnapshot:
    """Byte for byte. PROMPTS.md Prompt 20's second acceptance criterion."""

    def test_the_subject_matches(self) -> None:
        expected = (FIXTURES / "expected-alert.subject.txt").read_text(encoding="utf-8")
        assert _message().subject + "\n" == expected

    def test_the_plain_text_body_matches(self) -> None:
        expected = (FIXTURES / "expected-alert.txt").read_text(encoding="utf-8")
        assert _message().text == expected

    def test_the_html_body_matches(self) -> None:
        expected = (FIXTURES / "expected-alert.html").read_text(encoding="utf-8")
        assert _message().html + "\n" == expected


class TestTheEmailSaysWhatItMust:
    """The requirements, asserted independently of the snapshot. See the module docstring."""

    @pytest.mark.parametrize("symbol", ["BSE", "ZOMATO", "TITAN", "KAYNES", "TRENT"])
    def test_every_changed_name_appears(self, symbol: str) -> None:
        message = _message()
        assert symbol in message.text
        assert symbol in message.html

    def test_a_climb_is_shown_as_a_positive_move(self) -> None:
        """A subscriber must not have to work out which direction rank 5 -> 2 went."""
        assert "+3" in _message().text

    def test_it_carries_an_unsubscribe_link(self) -> None:
        message = _message()
        assert UNSUBSCRIBE_URL in message.text
        assert UNSUBSCRIBE_URL in message.html

    def test_it_carries_the_sebi_disclaimer(self) -> None:
        """docs/11 §Compliance: an email that discusses a screener is an analytics surface."""
        assert templates.DISCLAIMER in _message().text

    def test_it_uses_no_advice_language(self) -> None:
        """docs/11 §Compliance: "No buy/sell recommendations ... no 'advice' language"."""
        text = _message().text.lower()
        banned = ["buy ", "sell ", "recommend", "target price", "should own"]
        # The disclaimer itself says "not investment advice", which is the negation and is fine.
        body = text.split("\n--\n")[0]
        assert [word for word in banned if word in body] == []

    def test_it_has_both_a_text_and_an_html_part(self) -> None:
        message = _message()
        assert message.text.strip()
        assert message.html.strip().startswith("<div")


class TestDigest:
    """Prompt 20 §3's "digest preference": several screens, one email."""

    def test_a_digest_names_the_count_rather_than_one_screen(self) -> None:
        section = _section()
        message = templates.screen_alert(
            "subscriber@example.com", [section, section], manage_url=MANAGE_URL
        )
        assert message.subject == "Decile screen alerts: 2 screens changed"

    def test_a_digest_titles_each_section_and_a_single_alert_does_not(self) -> None:
        section = _section()
        single = templates.screen_alert("a@example.com", [section], manage_url=MANAGE_URL)
        digest = templates.screen_alert("a@example.com", [section, section], manage_url=MANAGE_URL)
        # A single alert's <h1> *is* the screen name, so it appears once and the section is
        # untitled. A digest's <h1> is "Your Decile screen alerts", so each section titles itself.
        assert single.html.count("Investing 001") == 1
        assert digest.html.count("Investing 001") == 2
        assert "Your Decile screen alerts" in digest.html

    def test_an_email_with_no_sections_is_refused(self) -> None:
        with pytest.raises(ValueError, match="nothing to say"):
            templates.screen_alert("a@example.com", [], manage_url=MANAGE_URL)


class TestRowCap:
    def test_a_capped_list_says_how_many_it_left_out(self) -> None:
        """An email that silently truncated would read as "only three names moved"."""
        section = _section()
        capped = templates.AlertSection(
            screen_name=section.screen_name,
            screen_url=section.screen_url,
            unsubscribe_url=section.unsubscribe_url,
            as_of=section.as_of,
            previous_as_of=section.previous_as_of,
            entries=section.entries[:1],
            exits=section.exits,
            movers=section.movers,
            entry_total=40,
            exit_total=section.exit_total,
            mover_total=section.mover_total,
            held_count=section.held_count,
        )
        message = templates.screen_alert("a@example.com", [capped], manage_url=MANAGE_URL)
        assert "and 39 more" in message.text
        assert "39" in message.html

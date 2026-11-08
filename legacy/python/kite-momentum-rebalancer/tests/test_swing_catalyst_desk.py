"""SW11B on the desk page: the catalyst is a link on triggers and plan lines, never text.

`docs/swing/STANDING-ANSWERS.md` A3 and the Track C §7 amendment, asserted on the rendered
page: a TRIGGERED signal for a name with an `sw_catalyst` row shows its newest headline as an
`<a target="_blank" rel="noopener">` to the exchange's copy, an earnings badge when the calendar
names a result, and nothing from any filing; a name with no row shows an em dash. The store's
read orders by stamp on sqlite exactly as it will on Postgres (undated rows last).

The `sw_catalyst` DDL (0032, in sqlite's spelling) lives in `test_swing_desk.py`'s list with
the rest of the schema, because `build_view` reads it on every render.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app import swing_desk
from tests.test_swing_desk import IST, NOW, USER, Scenario, client  # noqa: F401 - a fixture

FILING = "https://nsearchives.nseindia.com/corporate/ALPHAFLAG_02092026084105_PR.pdf?x=1&y=2"
CALENDAR = ("https://www.nseindia.com/companies-listing/corporate-filings-event-calendar"
            "?symbol=ALPHAFLAG")
TOKEN = {"present": False, "label": "-", "age_minutes": None, "expired": True}


@pytest.fixture()
def scenario(tmp_path):
    return Scenario(str(tmp_path / "swing.db"))


def _catalyst(scenario: Scenario, instrument_id: int, *, headline: str, url: str,
              published_at: dt.datetime | None, source: str = "NSE_ANNOUNCEMENT",
              earnings_date: dt.date | None = None) -> None:
    scenario.conn.execute(
        "INSERT INTO sw_catalyst(user_id, instrument_id, headline, published_at, url, source, "
        "earnings_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (USER, instrument_id, headline, published_at.isoformat() if published_at else None, url,
         source, earnings_date.isoformat() if earnings_date else None),
    )


def _view(scenario: Scenario) -> dict:
    return swing_desk.build_view(scenario.store(), now=NOW, dry_run=True, execution_enabled=False,
                                 monitor_enabled=False, token=TOKEN)


class TestTheStoreRead:
    def test_the_newest_announcement_and_the_earnings_date_per_name(self, scenario):
        scenario.config()
        older = dt.datetime(2026, 9, 1, 18, 32, 11, tzinfo=IST)
        newest = dt.datetime(2026, 9, 2, 8, 41, 5, tzinfo=IST)
        _catalyst(scenario, 1, headline="Updates — capex", url="https://a/older.pdf",
                  published_at=older)
        _catalyst(scenario, 1, headline="Press Release - deal", url=FILING, published_at=newest)
        _catalyst(scenario, 1, headline="No stamp", url="https://a/undated.pdf", published_at=None)
        _catalyst(scenario, 1, headline="Financial Results", url=CALENDAR, published_at=None,
                  source="NSE_EVENT_CALENDAR", earnings_date=dt.date(2026, 10, 15))
        got = scenario.store().catalysts_for([1, 2, 1])
        assert set(got) == {1}
        assert got[1] == {"headline": "Press Release - deal", "published_at": newest,
                          "url": FILING, "earnings_date": dt.date(2026, 10, 15)}

    def test_no_names_is_no_query(self, scenario):
        scenario.config()
        assert scenario.store().catalysts_for([]) == {}


class TestThePageLinksOut:
    def test_a_trigger_carries_its_catalyst_as_a_new_tab_link_and_an_earnings_badge(
            self, scenario, client):
        scenario.config()
        _catalyst(scenario, 1, headline="Press Release - deal", url=FILING,
                  published_at=dt.datetime(2026, 9, 2, 8, 41, 5, tzinfo=IST))
        _catalyst(scenario, 1, headline="Financial Results", url=CALENDAR, published_at=None,
                  source="NSE_EVENT_CALENDAR", earnings_date=dt.date(2026, 10, 15))
        scenario.signal(instrument_id=1, setup="FLAG", state="TRIGGERED",
                        raised_at=dt.datetime(2026, 9, 2, 9, 31, tzinfo=IST))
        scenario.signal(instrument_id=5, setup="FLAG", state="BELOW_PIVOT",
                        raised_at=dt.datetime(2026, 9, 2, 9, 35, tzinfo=IST))
        view = _view(scenario)
        by_symbol = {t["symbol"]: t["catalyst"] for t in view["triggers"]}
        assert by_symbol["ALPHAFLAG"]["url"] == FILING
        assert by_symbol["EPSILON"] is None

        html = client.get("/swing").text
        assert (f'<a class="sw-catalyst" href="{FILING.replace("&", "&amp;")}" target="_blank" '
                'rel="noopener noreferrer"') in html
        assert "Press Release - deal</a>" in html
        assert "earnings 15 Oct" in html
        assert "capex" not in html  # nothing but the newest headline; never a filing's text

    def test_a_plan_line_carries_the_link_and_a_name_without_one_shows_a_dash(
            self, scenario, client):
        ids = scenario.morning()
        _catalyst(scenario, 1, headline="Press Release - deal", url=FILING,
                  published_at=dt.datetime(2026, 9, 2, 8, 41, 5, tzinfo=IST))
        view = _view(scenario)
        buys = {ln["symbol"]: ln["catalyst"] for ln in view["plans"]["morning"]["buys"]}
        assert buys["ALPHAFLAG"]["headline"] == "Press Release - deal"
        assert all(c is None for symbol, c in buys.items() if symbol != "ALPHAFLAG")
        assert ids["morning"]

        html = client.get("/swing").text
        assert html.count('class="sw-catalyst"') >= 1
        assert "opens on nseindia.com" in html

    def test_the_page_names_no_placing_verb_for_the_feed(self):
        """Track C: a link can tell; it can never act. The catalyst macro has no form."""
        template = (Path(swing_desk.__file__).parent / "templates" / "swing.html"
                    ).read_text(encoding="utf-8")
        macro = template.split("{% macro catalyst(c) -%}", 1)[1].split("{%- endmacro %}", 1)[0]
        assert "<form" not in macro and "/swing/execute" not in macro
        assert 'target="_blank" rel="noopener noreferrer"' in macro

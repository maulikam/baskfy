"""The SQLite→Postgres translator — M19 §3.

This sits between 9,500 lines of money code and a database. A crash here is fixed on a Sunday; a
silent mistranslation is a wrong number that gets believed until quarter-end. So the tests are
about the cases where a naive implementation is quietly wrong rather than loudly broken.
"""

from __future__ import annotations

import pytest

from app.analytics import pg


class TestPlaceholders:
    def test_the_ordinary_case(self) -> None:
        assert pg.placeholders("select * from t where a=? and b=?") == (
            "select * from t where a=%s and b=%s"
        )

    def test_a_question_mark_inside_a_string_is_data(self) -> None:
        """`where note like '%?%'` is a search for a question mark, not a parameter."""
        assert pg.placeholders("select * from t where note = 'why?'") == (
            "select * from t where note = 'why?'"
        )

    def test_an_escaped_quote_does_not_end_the_string(self) -> None:
        """`'it''s'` is one string containing an apostrophe. A naive scanner sees two strings and
        then treats everything after as code — including any `?` in it."""
        sql = "select * from t where s = 'it''s ok?' and a = ?"
        assert pg.placeholders(sql) == "select * from t where s = 'it''s ok?' and a = %s"

    def test_percent_is_escaped_for_psycopg(self) -> None:
        """psycopg reads `%` as its own format character, so valid SQL raises without this."""
        assert pg.placeholders("select * from t where s like '%x%'") == (
            "select * from t where s like '%%x%%'"
        )

    def test_percent_outside_a_string_is_escaped_too(self) -> None:
        assert "%%" in pg.placeholders("select 10 % 3")


class TestDialect:
    def test_autoincrement_becomes_bigserial(self) -> None:
        out = pg.translate("create table t (id INTEGER PRIMARY KEY AUTOINCREMENT, x text)")
        assert "BIGSERIAL PRIMARY KEY" in out
        assert "AUTOINCREMENT" not in out

    def test_insert_or_replace_loses_its_sqlite_spelling(self) -> None:
        assert pg.translate("INSERT OR REPLACE INTO t VALUES (?)").startswith("INSERT INTO")

    def test_strftime_becomes_to_char(self) -> None:
        out = pg.translate("select strftime('%Y-%m', trade_date) from trades")
        assert "to_char((trade_date)::date, 'YYYY-MM')" in out
        assert "strftime" not in out

    def test_an_unknown_strftime_pattern_raises_rather_than_guessing(self) -> None:
        """A wrongly translated date pattern is a report that disagrees with itself in July."""
        with pytest.raises(ValueError, match="no Postgres translation"):
            pg.translate("select strftime('%j', d) from t")


class TestRow:
    def test_it_reads_by_name(self) -> None:
        assert pg.Row({"a": 1, "b": 2})["a"] == 1

    def test_it_reads_by_position(self) -> None:
        """`conn.execute("select count(*)").fetchone()[0]` appears throughout the desk."""
        assert pg.Row({"count": 7})[0] == 7

    def test_dict_of_it_is_a_plain_dict(self) -> None:
        assert dict(pg.Row({"a": 1})) == {"a": 1}

    def test_keys_works(self) -> None:
        assert list(pg.Row({"a": 1, "b": 2}).keys()) == ["a", "b"]


def test_lastrowid_refuses_rather_than_approximating() -> None:
    """Postgres has no lastrowid. Returning a plausible integer is how a fill gets attached to
    the wrong trade, so the adapter raises and the four call sites must use RETURNING."""

    class _Fake:
        description = None

    with pytest.raises(NotImplementedError, match="RETURNING"):
        _ = pg.Cursor(_Fake()).lastrowid


def test_translation_is_not_applied_twice() -> None:
    """`translate` calls `placeholders`. Running it again must not turn `%s` into `%%s`."""
    once = pg.translate("select * from t where a=? and s like '%x%'")
    assert "%s" in once
    assert once.count("%%") == 2      # the two literal percents, and not the placeholder

"""TW12's table: `tw_scan_run`, the model and the migration held equal.

`packages/core/tests/test_twt_schema.py` already walks every `tw_` table for the things that are
true of all of them — a non-null `user_id`, a cascading foreign key, a presence in both halves of
the migration. This file is about the three things that are true of *this* table and would not be
caught there:

1. **the status vocabulary is the swing book's, word for word and in the same order.** A page
   written against "Scan now" on `/swing` must work on `/twt` without learning a second set of
   strings, which is what `PLAN-SCAN-SYNC.md`'s contract means by "the status vocabulary is the
   swing one";
2. **the database refuses a word the code does not emit, and vice versa.** One definition, two
   consumers — the rule `models/vbt.py`'s header states and the reason these tuples are imported
   rather than retyped;
3. **`0042_twt_scan_run` chains onto `0041_twt` and undoes exactly itself.** The round trip is
   proved against a real database in `gates/twt-scan-now.md` G2/G2a; this is the cheap half that
   runs with no Postgres, and it is the half that catches a revision id typed twice.

There is a fourth, and it is the one worth saying out loud: **there is no `provisional` column.**
It is asserted as an absence because absence is what somebody copying `sw_scan_run` would get
wrong, and a column that is always false is a promise the strategy cannot keep
(DECISIONS-TW TW12.2).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import CheckConstraint

from baskfy_core.models import TW_SCAN_SOURCES, TW_SCAN_STATUSES, TwScanRun
from baskfy_core.models.base import Base
from baskfy_core.models.swing import SW_SCAN_STATUSES

REPO: Final = Path(__file__).resolve().parents[3]
MIGRATION_PATH: Final = REPO / "services" / "api" / "alembic" / "versions" / "0042_twt_scan_run.py"
MIGRATION: Final = MIGRATION_PATH.read_text(encoding="utf-8")

#: Through the metadata rather than ``TwScanRun.__table__``, which is typed ``FromClause`` and
#: so has neither ``constraints`` nor ``indexes`` as far as a type checker is concerned. The same
#: route ``test_twt_schema.py`` takes, for the same reason.
TABLE: Final = Base.metadata.tables["tw_scan_run"]


class TestTheVocabulary:
    def test_the_statuses_are_the_swing_book_s_four_in_the_same_order(self) -> None:
        """`PLAN-SCAN-SYNC.md`: "Status vocabulary is the swing one: QUEUED | RUNNING | DONE |
        FAILED." Compared against the swing book's own tuple rather than a literal, so the two
        cannot drift apart in a later edit to either."""
        assert TW_SCAN_STATUSES == ("QUEUED", "RUNNING", "DONE", "FAILED")
        assert TW_SCAN_STATUSES == SW_SCAN_STATUSES

    def test_the_sources_say_which_button_was_pressed(self) -> None:
        """`vb_scan_run` admits `desk` and `cli`; this one also admits `web`, because the `/twt`
        hub is getting the button too (DECISIONS-TW TW12.3). A row that cannot say where a
        request came from is a row that cannot be audited afterwards."""
        assert TW_SCAN_SOURCES == ("desk", "web", "cli")

    @pytest.mark.parametrize("status", TW_SCAN_STATUSES)
    def test_every_status_the_code_emits_is_in_the_migration_s_constraint(
        self, status: str
    ) -> None:
        assert f"'{status}'" in MIGRATION, f"{status} is not in a CHECK constraint"

    @pytest.mark.parametrize("source", TW_SCAN_SOURCES)
    def test_every_source_the_code_emits_is_in_the_migration_s_constraint(
        self, source: str
    ) -> None:
        assert f"'{source}'" in MIGRATION, f"{source} is not in a CHECK constraint"

    def test_the_constraint_admits_nothing_the_code_does_not_emit(self) -> None:
        """The other direction, and the one a copy-paste gets wrong: a constraint permitting a
        state no job writes is a column nobody can trust."""
        found = re.search(r"status IN \(([^)]*)\)", MIGRATION)
        assert found is not None
        admitted = tuple(word.strip().strip("'") for word in found.group(1).split(","))
        assert admitted == TW_SCAN_STATUSES


class TestTheTableItself:
    def test_it_is_named_what_the_document_says(self) -> None:
        assert TwScanRun.__tablename__ == "tw_scan_run"

    def test_it_carries_no_provisional_column(self) -> None:
        """DECISIONS-TW TW12.2, as an assertion rather than a comment.

        `sw_scan_run` has one because the swing book's setups can be read off a bar still being
        formed. `04` §2 measures three *closed* weekly ranges, so there is nothing provisional to
        record and a column that is always false would be a promise this strategy cannot keep.
        """
        assert "provisional" not in TABLE.c

    def test_the_columns_are_the_ones_the_migration_creates(self) -> None:
        expected = {
            "id",
            "user_id",
            "requested_at",
            "started_at",
            "finished_at",
            "session_date",
            "status",
            "source",
            "detail",
            "error",
            "task_id",
            "created_at",
        }
        assert set(TABLE.c.keys()) == expected
        for name in expected:
            assert f'"{name}"' in MIGRATION, f"{name} is on the model and not in the migration"

    def test_only_the_columns_the_worker_fills_in_later_are_nullable(self) -> None:
        """A scan that has not run yet knows its user and its time and nothing else; a scan that
        has run knows the rest. Nullability *is* that distinction, so it is asserted rather than
        left to whoever writes the next column."""
        nullable = {name for name, column in TABLE.c.items() if column.nullable}
        assert nullable == {
            "started_at",
            "finished_at",
            "session_date",
            "detail",
            "error",
            "task_id",
        }

    def test_a_finish_cannot_precede_a_start(self) -> None:
        """The one constraint here that is a rule rather than hygiene: a row whose `finished_at`
        is before its `started_at` is a row nobody can read a duration off."""
        checks = [c for c in TABLE.constraints if isinstance(c, CheckConstraint)]
        names = {c.name for c in checks}
        # The naming convention in `models/base.py` prefixes `ck_<table>_`, so the constraint
        # arrives as `ck_tw_scan_run_finished_after_started` — the same spelling `op.f(...)` gives
        # it in the migration, which is what makes the two halves comparable at all.
        assert "ck_tw_scan_run_finished_after_started" in names, names
        assert "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at" in MIGRATION

    def test_the_query_the_two_refusals_make_is_indexed(self) -> None:
        """Both refusals read "this user's newest request". On an append-only table that becomes
        a sequential scan the week after it ships."""
        assert any(
            index.name == "ix_tw_scan_run_user_id_requested_at"
            and [column.name for column in index.columns] == ["user_id", "requested_at"]
            for index in TABLE.indexes
        )
        assert "ix_tw_scan_run_user_id_requested_at" in MIGRATION


class TestTheMigrationChain:
    def test_it_is_the_next_number_and_chains_onto_the_twt_migration(self) -> None:
        assert 'revision = "0042_twt_scan_run"' in MIGRATION
        assert 'down_revision = "0041_twt"' in MIGRATION

    def test_no_other_version_file_claims_0042(self) -> None:
        """Two heads is the symptom a concurrent session leaves behind, and it is cheaper to
        catch here than in `alembic upgrade`."""
        siblings = sorted(MIGRATION_PATH.parent.glob("0042*.py"))
        assert [path.name for path in siblings] == ["0042_twt_scan_run.py"]

    def test_the_downgrade_undoes_exactly_the_upgrade(self) -> None:
        """A downgrade that does not undo is worse than none: it is the path taken at 3am. The
        real proof is `gates/twt-scan-now.md` G2a against a throwaway database; this catches the
        drop that was never written."""
        assert 'op.create_table(\n        "tw_scan_run"' in MIGRATION
        assert 'op.drop_table("tw_scan_run")' in MIGRATION
        assert 'op.create_index(\n        "ix_tw_scan_run_user_id_requested_at"' in MIGRATION
        assert (
            'op.drop_index("ix_tw_scan_run_user_id_requested_at", table_name="tw_scan_run")'
            in MIGRATION
        )

"""The restore drill's assertions, and the `/metrics` endpoint (Prompt 17 deliverables 2 and 6).

``baskfy_api.integrity`` is what turns "``pg_restore`` exited 0" into "this database could serve
the application". It is asserted here against the *seeded* test database, which is the same shape
the drill restores — and, more importantly, against deliberately broken states, because an
integrity check that only ever sees a healthy database is a check nobody has watched fail.
"""

from __future__ import annotations

import pytest
from api_helpers import api_settings, running_app
from screener_helpers import requires_db
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import metrics
from baskfy_api.integrity import CheckStatus, run_integrity_checks

pytestmark = [pytest.mark.db, requires_db]

#: The smallest definition the screener accepts — same shape `test_api_run` uses.
MINIMAL = {"index": "nifty-total-market", "sort_by": "avg_sharpe_12_6_3_1", "series": ["EQ", "BE"]}


def _cache_events(result: str) -> float:
    """The counter's current value, or 0 before it has ever been incremented."""
    return (
        metrics.REGISTRY.get_sample_value("baskfy_screen_cache_events_total", {"result": result})
        or 0.0
    )


class TestTheIntegrityAssertions:
    async def test_a_seeded_database_passes_every_assertion(
        self, screener_session: AsyncSession
    ) -> None:
        """The control. The drill asserts the *source* first for exactly this reason."""
        report = await run_integrity_checks(screener_session)
        failures = [f"{result.name}: {result.message}" for result in report.failures]
        assert failures == []
        assert report.passed

    async def test_it_names_the_alembic_revision(self, screener_session: AsyncSession) -> None:
        """A dump taken before a migration is a valid backup and an invalid deployment."""
        report = await run_integrity_checks(screener_session)
        alembic = next(r for r in report.results if r.name == "alembic_revision")
        assert alembic.status is CheckStatus.PASSED
        assert alembic.observed["revision"]

    async def test_the_hypertables_are_hypertables(self, screener_session: AsyncSession) -> None:
        """docs/02 locks TimescaleDB; a restore without it silently loses the chunking."""
        report = await run_integrity_checks(screener_session)
        hypertables = next(r for r in report.results if r.name == "hypertables")
        assert hypertables.status is CheckStatus.PASSED
        found = hypertables.observed["found"]
        assert isinstance(found, list)
        assert "ohlcv_daily" in found

    async def test_a_missing_table_fails_the_report(self, screener_session: AsyncSession) -> None:
        """The assertion that would actually catch a truncated restore.

        Rolled back with the enclosing test transaction, so the database is unchanged afterwards.
        """
        await screener_session.execute(text("DROP TABLE consent_record CASCADE"))
        report = await run_integrity_checks(screener_session)
        assert not report.passed
        tables = next(r for r in report.results if r.name == "tables_present")
        assert tables.status is CheckStatus.FAILED
        assert "consent_record" in tables.message

    async def test_an_orphan_row_fails_the_report(self, screener_session: AsyncSession) -> None:
        """A `pipeline_run_step` whose run does not exist — the shape a partial restore leaves."""
        await screener_session.execute(
            text(
                "INSERT INTO pipeline_run (trade_date, status, started_at) "
                "VALUES ('2026-08-18', 'succeeded', now())"
            )
        )
        run_id = (
            await screener_session.execute(text("SELECT max(id) FROM pipeline_run"))
        ).scalar_one()
        await screener_session.execute(
            text(
                "INSERT INTO pipeline_run_step (run_id, step, status) "
                "VALUES (:id, 'publish', 'succeeded')"
            ),
            {"id": run_id},
        )
        # Break the link the foreign key would normally protect. `ON DELETE CASCADE` means the
        # step would go too, so the constraint is dropped for the length of this transaction.
        await screener_session.execute(
            text(
                "ALTER TABLE pipeline_run_step "
                "DROP CONSTRAINT fk_pipeline_run_step_run_id_pipeline_run"
            )
        )
        await screener_session.execute(
            text("DELETE FROM pipeline_run WHERE id = :id"), {"id": run_id}
        )

        report = await run_integrity_checks(screener_session)
        orphans = next(r for r in report.results if r.name == "steps_reference_runs")
        assert orphans.status is CheckStatus.FAILED
        assert orphans.observed["violations"] == 1

    async def test_duplicate_data_versions_fail(self, screener_session: AsyncSession) -> None:
        """docs/06 §Caching keys every cached result on `data_version`. It must be unique."""
        await screener_session.execute(
            text(
                "INSERT INTO pipeline_run (trade_date, status, started_at, data_version) VALUES "
                "('2026-08-17', 'succeeded', now(), 99), "
                "('2026-08-18', 'succeeded', now(), 99)"
            )
        )
        report = await run_integrity_checks(screener_session)
        versions = next(r for r in report.results if r.name == "data_version_unique")
        assert versions.status is CheckStatus.FAILED
        # The seeded fixture already published one run, so the counts are relative rather than
        # absolute: what matters is that two runs share a version.
        published = versions.observed["published"]
        distinct = versions.observed["distinct"]
        assert isinstance(published, int) and isinstance(distinct, int)
        assert distinct == published - 1

    async def test_a_published_run_must_carry_its_step_history(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/03: "Every step writes a row in `pipeline_run_step`".

        A published run with no step rows is a run whose audit trail did not survive — which is
        exactly the shape of a restore that lost a table. The seeded fixture writes the `publish`
        step for its own fabricated run (`baskfy_api.seed.seed_published_run`) precisely so this
        assertion is not tripped by the seed rather than by a bad backup.
        """
        report = await run_integrity_checks(screener_session)
        steps = next(r for r in report.results if r.name == "published_runs_have_steps")
        assert steps.status is CheckStatus.PASSED

        await screener_session.execute(text("DELETE FROM pipeline_run_step"))
        broken = await run_integrity_checks(screener_session)
        assert not broken.passed
        assert (
            next(r for r in broken.results if r.name == "published_runs_have_steps").status
            is CheckStatus.FAILED
        )

    async def test_a_skip_is_not_a_pass(self, screener_session: AsyncSession) -> None:
        """ "We did not look" and "we looked and it was fine" are different facts.

        On a database that has never published there is no `data_version` history to check, and
        the report says SKIP rather than PASS — a drill summary that rendered those as PASS would
        be claiming to have checked something it could not. A skip still does not *fail* the
        report: a freshly migrated database must go green.
        """
        await screener_session.execute(text("DELETE FROM pipeline_run_step"))
        await screener_session.execute(text("DELETE FROM pipeline_run"))
        report = await run_integrity_checks(screener_session)
        skipped = {r.name for r in report.results if r.status is CheckStatus.SKIPPED}
        assert skipped == {"data_version_unique", "published_runs_have_steps"}
        assert report.passed

    async def test_the_payload_is_json_serialisable(self, screener_session: AsyncSession) -> None:
        """The CI job writes it to the step summary with `--json`."""
        import json  # noqa: PLC0415

        report = await run_integrity_checks(screener_session)
        round_tripped = json.loads(json.dumps(report.to_payload()))
        assert round_tripped["passed"] is True
        assert len(round_tripped["results"]) == len(report.results)


class TestTheMetricsEndpoint:
    async def test_it_serves_prometheus_text(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        settings = api_settings(seeded_url)
        async with running_app(settings, screener_session) as client:
            response = await client.get("/metrics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "baskfy_http_request_duration_seconds" in response.text

    async def test_it_is_outside_the_versioned_api(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        """It is not part of docs/07's surface and must never reach the generated client."""
        settings = api_settings(seeded_url)
        async with running_app(settings, screener_session) as client:
            document = await client.get("/api/v1/openapi.json")
            versioned = await client.get("/api/v1/metrics")

        assert "/metrics" not in document.json()["paths"]
        assert versioned.status_code == 404

    async def test_a_token_can_be_required(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        """Correct behind a private network, wrong on the public internet — so it is a setting."""
        settings = api_settings(seeded_url, metrics_token="scrape-me-please")
        async with running_app(settings, screener_session) as client:
            refused = await client.get("/metrics")
            wrong = await client.get("/metrics", headers={"Authorization": "Bearer nope"})
            allowed = await client.get(
                "/metrics", headers={"Authorization": "Bearer scrape-me-please"}
            )

        assert refused.status_code == 401
        assert wrong.status_code == 401
        assert allowed.status_code == 200

    async def test_it_can_be_switched_off(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        settings = api_settings(seeded_url, metrics_enabled=False)
        async with running_app(settings, screener_session) as client:
            response = await client.get("/metrics")
        assert response.status_code == 404

    async def test_a_request_is_labelled_with_its_route_template(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        """The cardinality guarantee, end to end rather than in a unit test."""
        settings = api_settings(seeded_url)
        async with running_app(settings, screener_session) as client:
            await client.get("/api/v1/meta/status")
            response = await client.get("/metrics")

        assert 'route="/api/v1/meta/status"' in response.text

    async def test_the_pipeline_gauges_are_refreshed_from_the_database(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        """docs/DECISIONS.md §17.9: the durable answer is `pipeline_run`, read at scrape time.

        Asserted against the **committed** seed rather than a row inserted here, and that is the
        point rather than a compromise: `/metrics` refreshes its gauges from
        ``app.state.session_factory`` — a connection of its own — so it cannot see this test's
        open transaction. A scrape that could would be reading uncommitted state.
        """
        settings = api_settings(seeded_url)
        async with running_app(settings, screener_session) as client:
            response = await client.get("/metrics")

        # `screener_helpers` seeds exactly one published run, at version 1.
        assert "baskfy_data_version 1.0" in response.text
        assert 'baskfy_pipeline_run_status{status="succeeded"} 1.0' in response.text
        assert 'baskfy_pipeline_run_status{status="failed"} 0.0' in response.text
        # Its `publish` step is what `baskfy_api.seed.seed_published_run` writes.
        assert 'baskfy_pipeline_last_step_duration_seconds{step="publish"}' in response.text

    @pytest.mark.redis
    async def test_a_screen_run_counts_a_miss_and_then_a_hit(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        """docs/06 §Caching's hit rate is only meaningful if both outcomes are counted.

        `clean_redis_namespaces` is what makes the first run a *miss*: without it the key is
        already warm from whatever ran before, and the test would assert a hit while believing it
        had proved a miss.
        """
        del clean_redis_namespaces
        misses = _cache_events("miss")
        hits = _cache_events("hit")

        settings = api_settings(seeded_url)
        async with running_app(settings, screener_session) as client:
            cold = await client.post("/api/v1/screens/preview", json={"definition": MINIMAL})
            warm = await client.post("/api/v1/screens/preview", json={"definition": MINIMAL})

        assert cold.status_code == 200, cold.text
        assert warm.status_code == 200, warm.text
        assert _cache_events("miss") == misses + 1
        assert _cache_events("hit") == hits + 1

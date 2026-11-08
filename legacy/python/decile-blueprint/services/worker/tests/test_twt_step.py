"""TW4's fourteenth step: `compute_twt`, and the one guarantee it exists to give.

``docs/twt/06-module-plan.md`` § TW4: *"A new `PipelineStep.COMPUTE_TWT` **after `COMPUTE_VBT`**,
added to `POST_PUBLISH_STEPS` and wrapped by `run_compute_twt_step`, which — like its two
siblings — **cannot raise**."*

Four claims:

1. **A detector that raises leaves the run SUCCEEDED.** Not "usually", not "unless it is an
   IntegrityError": a test makes it raise and the chain carries on. A `tw_state_daily` row nobody
   wrote is a page saying "nothing is tight today"; a nightly run that failed is a screener
   serving yesterday to everybody, and the trade is not close.
2. **It runs after `compute_vbt`, which runs after `compute_swing`.** Asserted on the chain
   itself, and asserted as *membership of* `POST_PUBLISH_STEPS` rather than as "it is last" —
   `steps.py` says why where that set is defined, and TW4 is the module that made the difference
   matter for the second time.
3. **No sole tenant, or the flag off, is a skip that says so** — never an invented user and never
   a silent pass.
4. **Neither sibling is touched.** `compute_swing` and `compute_vbt` are where they were, still
   post-publish, and this step writes no row of either sleeve.

The step's *body* — what it detects, what it writes, what it ratchets — is
`services/api/tests/test_twt_detect.py`. This file is only about the wrapper, which is a separate
guarantee and is why the wrapper is a function rather than a block inside `_run_chain`.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa
from helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import AppUser, PipelineRun, PipelineRunStep, TwConfig
from baskfy_worker.deps import PipelineDependencies
from baskfy_worker.orchestrator import run_compute_twt_step
from baskfy_worker.steps import POST_PUBLISH_STEPS, PipelineStep, StepStatus
from baskfy_worker.tasks import twt as twt_task

pytestmark = requires_db

AS_OF = dt.date(2026, 8, 18)


async def _sole_user(session: AsyncSession) -> int:
    user = AppUser(public_id="tw4-step", email="tw4-step@example.com")
    session.add(user)
    await session.flush()
    session.add(TwConfig(user_id=user.id, updated_by="test"))
    await session.flush()
    return int(user.id)


async def _run(session: AsyncSession) -> int:
    run = PipelineRun(trade_date=AS_OF, status="running", started_at=dt.datetime.now(dt.UTC))
    session.add(run)
    await session.flush()
    return int(run.id)


async def _step(session: AsyncSession, run_id: int) -> PipelineRunStep:
    return (
        await session.execute(
            sa.select(PipelineRunStep).where(
                PipelineRunStep.run_id == run_id,
                PipelineRunStep.step == PipelineStep.COMPUTE_TWT.value,
            )
        )
    ).scalar_one()


@pytest.mark.db
class TestTheStepCannotFailTheNight:
    async def test_a_detector_that_raises_is_recorded_and_the_step_returns(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Claim 1, made by actually making it raise.

        A guarantee that is only written down is a comment. `run_compute_twt_step` swallows the
        exception, records its type and message on the step row, and returns — so the
        orchestrator walks on and `data_version` is not held back by a sleeve nothing downstream
        depends on.
        """
        user_id = await _sole_user(session)
        run_id = await _run(session)

        async def _explode(*_args: object, **_kwargs: object) -> int:
            raise RuntimeError("the three-weeks-tight detector fell over")

        monkeypatch.setattr(twt_task, "run_detect_twt", _explode)
        await run_compute_twt_step(
            session, run_id, AS_OF, PipelineDependencies(provider=None, twt_user_id=user_id)
        )

        step = await _step(session, run_id)
        assert step.status == StepStatus.SKIPPED.value
        assert "RuntimeError" in str(step.error)
        assert "fell over" in str(step.error)

    async def test_the_step_is_one_the_run_does_not_depend_on(self) -> None:
        """Claim 1 as a property of the chain rather than of one run.

        `POST_PUBLISH_STEPS` is the declared list of steps whose failure the run survives, and
        `steps.py` asks for a step added after them to *join the set or be a step the run's
        success depends on* — "a decision, not an edit". DECISIONS-TW **TW4.5** is the decision.
        """
        assert PipelineStep.COMPUTE_TWT in POST_PUBLISH_STEPS

    async def test_no_sole_tenant_skips_rather_than_inventing_one(
        self, session: AsyncSession
    ) -> None:
        """The `tw_` schema is keyed by user and a nightly job may not invent one."""
        run_id = await _run(session)
        await run_compute_twt_step(
            session, run_id, AS_OF, PipelineDependencies(provider=None, twt_user_id=None)
        )
        step = await _step(session, run_id)
        assert step.status == StepStatus.SKIPPED.value
        assert "BASKFY_SOLE_USER_ID" in str(step.error)

    async def test_the_flag_off_skips_the_step(self, session: AsyncSession) -> None:
        """`docs/twt/02` Track B's one on-by-default flag, off: the step says so and does
        nothing. It is not removed, because a sleeve with no history is a sleeve with no
        evidence."""
        user_id = await _sole_user(session)
        run_id = await _run(session)
        await run_compute_twt_step(
            session,
            run_id,
            AS_OF,
            PipelineDependencies(provider=None, twt_user_id=user_id, twt_nightly_enabled=False),
        )
        step = await _step(session, run_id)
        assert step.status == StepStatus.SKIPPED.value
        assert "NIGHTLY" in str(step.error)

    async def test_the_execution_flag_is_not_read_by_the_step_at_all(
        self, session: AsyncSession
    ) -> None:
        """Non-negotiable 1, and `docs/twt/02` Track C §3: nothing in detection is a switch.

        With ``twt_execution_enabled`` true the step behaves **identically** — it places nothing,
        arms nothing and sizes nothing, because there is no code path here that could. The two
        runs are compared on what they wrote rather than on what they were told.
        """
        user_id = await _sole_user(session)
        off = await _run(session)
        await run_compute_twt_step(
            session,
            off,
            AS_OF,
            PipelineDependencies(provider=None, twt_user_id=user_id),
        )
        on = await _run(session)
        await run_compute_twt_step(
            session,
            on,
            AS_OF,
            PipelineDependencies(provider=None, twt_user_id=user_id, twt_execution_enabled=True),
        )
        assert (await _step(session, off)).status == (await _step(session, on)).status
        for table in ("tw_order", "tw_fill", "tw_plan", "tw_plan_line"):
            count = (
                await session.execute(sa.select(sa.func.count()).select_from(sa.table(table)))
            ).scalar_one()
            assert count == 0, f"detection wrote a {table} row"


@pytest.mark.db
class TestTheChainOrder:
    async def test_compute_twt_comes_after_compute_vbt_in_the_chain(self) -> None:
        """Claim 2. `06` § TW4 names the position and this is the test that holds it."""
        chain = list(PipelineStep)
        assert chain.index(PipelineStep.COMPUTE_SWING) < chain.index(PipelineStep.COMPUTE_VBT)
        assert chain.index(PipelineStep.COMPUTE_VBT) < chain.index(PipelineStep.COMPUTE_TWT)

    async def test_everything_after_publish_in_the_chain_is_a_post_publish_step(self) -> None:
        """The property `steps.py` asks for, stated over the whole tail rather than over one
        step: a step added after `publish` that the run's success *does* depend on would be a
        nightly that a sleeve can fail."""
        chain = list(PipelineStep)
        tail = chain[chain.index(PipelineStep.PUBLISH) + 1 :]
        assert set(tail) == POST_PUBLISH_STEPS
        assert tail[-1] is PipelineStep.COMPUTE_TWT

    async def test_neither_sibling_step_moved(self) -> None:
        """Claim 4. `compute_swing` is twelfth and `compute_vbt` thirteenth, as they were."""
        chain = [step.value for step in PipelineStep]
        assert chain[10:14] == [
            "refresh_basket",
            "compute_swing",
            "compute_vbt",
            "compute_twt",
        ]

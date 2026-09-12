"""AUDIT 4.16 — generic bad-request problem type exists and is used for non-screen 400s."""

from __future__ import annotations

from baskfy_api.problems import ProblemType, STATUS_FOR, bad_request


def test_bad_request_is_a_400_distinct_from_invalid_screen() -> None:
    assert ProblemType.BAD_REQUEST.value == "bad-request"
    assert STATUS_FOR[ProblemType.BAD_REQUEST] == 400
    problem = bad_request("nope")
    assert problem.type is ProblemType.BAD_REQUEST
    assert problem.status == 400

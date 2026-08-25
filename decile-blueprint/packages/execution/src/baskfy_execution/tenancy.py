"""Law 2's multi-tenant clause, as values the gateway can check (docs/05 P4.3).

Every order carries a ``user_id`` and a ``broker_account_id``. The gateway refuses any
order whose plan was built for a different pair. This module is pure: no database, no
network, no clock — execution still does not import ``baskfy_core``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["TenantIds", "refuse_cross_tenant"]


@dataclass(frozen=True, slots=True)
class TenantIds:
    """The two identifiers Law 2 requires on every order."""

    user_id: int
    broker_account_id: int

    def __post_init__(self) -> None:
        if self.user_id < 1 or self.broker_account_id < 1:
            raise ValueError("tenant ids must be positive")


def refuse_cross_tenant(caller: TenantIds, plan: TenantIds) -> str | None:
    """Return a refusal message when the plan was not built for this caller.

    ``None`` means the pair matches. A message means BLOCKED — never an exception the
    caller could turn into a 500 (P4.3).
    """
    if caller.user_id != plan.user_id or caller.broker_account_id != plan.broker_account_id:
        return (
            "tenant mismatch: caller "
            f"({caller.user_id},{caller.broker_account_id}) != plan "
            f"({plan.user_id},{plan.broker_account_id})"
        )
    return None

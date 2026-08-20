"""Request and response models — the wire contract of docs/07 (Prompt 7 deliverable 2).

These are what `openapi.json` is generated from, and therefore what `packages/api-client`'s
TypeScript is generated from. docs/02 rule 5: "Typed end to end. Pydantic models -> OpenAPI ->
generated TS client. No hand-written fetch types."

A note on numbers
-----------------
The screen-run payload is **not** serialised by these models. ``ScreenResult.to_json`` produces
it (docs/06 §"Determinism guarantee": byte-identical results), and the route returns those bytes
verbatim so that the API, the CSV export and the UI cannot disagree about a rounded value —
CLAUDE.md house rule 8. :class:`ScreenRunResponse` exists to *document* that payload in OpenAPI,
which is what gives the generated client its types.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, JsonValue

from decile_core.screen_definition import ScreenDefinition

#: docs/07 §Conventions: "Cursor pagination: `?limit=100&cursor=…`".
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500

Limit = Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)]


class _Out(BaseModel):
    """Response models are closed: an accidental extra field is a contract change."""

    model_config = ConfigDict(extra="forbid")


class _In(BaseModel):
    """docs/07 §Screens: "Unknown keys are rejected (`extra="forbid"`)"."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


class FactorOut(_Out):
    """docs/07: "factor registry (key, label, family, unit, higher_is_better)"."""

    key: str
    label: str
    family: str
    unit: str
    higher_is_better: bool


class ColumnOut(_Out):
    """docs/07: "the 34 selectable result columns" — docs/01 §4 in fact enumerates 36."""

    key: str
    label: str
    unit: str
    is_factor: bool


class UniverseOut(_Out):
    """docs/07: "index_def rows where is_universe"."""

    index_id: int
    slug: str
    name: str
    sort_order: int
    market_health: bool


class TradingDaysOut(_Out):
    """docs/07: "list of trading dates (for the historical date picker)"."""

    from_: dt.date = Field(alias="from")
    to: dt.date
    dates: list[dt.date]

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PipelineRunOut(_Out):
    trade_date: dt.date
    status: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    data_version: int | None


class StatusOut(_Out):
    """docs/07: "{ as_of, data_version, last_pipeline_run }"."""

    as_of: dt.date | None
    data_version: int
    last_pipeline_run: PipelineRunOut | None
    #: docs/11 §Reliability: "if the pipeline fails, serve the last good `data_version` with a
    #: banner". This is the flag the banner reads.
    degraded: bool
    data_start_date: dt.date


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


class ScreenOut(_Out):
    public_id: str
    name: str
    definition: ScreenDefinition
    columns: list[str]
    is_example: bool
    #: False for example screens and for other people's — the UI greys out Save.
    editable: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class ScreenListOut(_Out):
    """docs/07: "GET /screens -> user screens + example screens"."""

    data: list[ScreenOut]


class ScreenCreate(_In):
    name: str = Field(min_length=1, max_length=120)
    definition: ScreenDefinition
    columns: list[str] | None = None


class ScreenUpdate(_In):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    definition: ScreenDefinition | None = None
    columns: list[str] | None = None


class ScreenDuplicate(_In):
    name: str | None = Field(default=None, min_length=1, max_length=120)


# ---------------------------------------------------------------------------
# Running a screen
# ---------------------------------------------------------------------------


class RunRequest(_In):
    """docs/07: `{ "as_of": "2026-08-19" | null, "override_definition": {…} | null }`."""

    as_of: dt.date | None = None
    override_definition: ScreenDefinition | None = None
    #: Not in docs/07's request body, but required by its own error catalogue: "409
    #: `stale-data-version` — client sent a `data_version` that no longer exists". The client
    #: sends the version it believes is current (from `/meta/status` or a previous response); if
    #: the snapshot has moved on, it gets a 409 instead of rows it would mis-attribute. See
    #: docs/07a §2.
    data_version: int | None = Field(default=None, ge=0)


class PreviewRequest(_In):
    """docs/07: "POST /screens/preview — run an unsaved definition"."""

    definition: ScreenDefinition
    as_of: dt.date | None = None
    columns: list[str] | None = None
    data_version: int | None = Field(default=None, ge=0)


class SortingFactorOut(_Out):
    key: str
    label: str


class ScreenRunRowOut(BaseModel):
    """One result row: ``rank`` plus one member per entry of ``columns``.

    ``extra="allow"`` because the column set is the user's, not the schema's — which is what
    docs/01 §4's column picker means. In OpenAPI this becomes ``additionalProperties``, so the
    generated client types the known keys and permits the rest.
    """

    model_config = ConfigDict(extra="allow")

    rank: int
    symbol: str
    name: str
    sorting_factor: JsonValue = None


class ScreenRunResponse(_Out):
    """docs/07 §"Running a screen", field for field."""

    as_of: dt.date
    data_version: int
    result_count: int
    sorting_factor: SortingFactorOut
    columns: list[str]
    rows: list[ScreenRunRowOut]


class ScreenRunSummaryOut(_Out):
    """docs/07: "GET /screens/{public_id}/runs?limit= -> historical run summaries"."""

    as_of: dt.date
    definition_hash: str
    result_count: int
    created_at: dt.datetime


class ScreenRunPage(_Out):
    """docs/07 §Conventions: `{ "data": [...], "next_cursor": "…" }`."""

    data: list[ScreenRunSummaryOut]
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Instruments (docs/07 §Instruments, docs/01 §5)
# ---------------------------------------------------------------------------

#: One displayable value from a fact row.
#:
#: `Decimal` rather than `float` (CLAUDE.md house rule 9) and rather than `JsonValue` (which has
#: no `Decimal` member) because the stored precision *is* the contract — house rule 8. These
#: responses are serialised with `decile_core.screener.canonical_json`, the same encoder the
#: screener uses, so `Decimal("13.00")` reaches the browser as `13.00` and not as `13.0`.
#:
#: The other members are the non-numeric cells the documented blocks contain: `str` for Series,
#: `date` for Listed On, `int` for a circuit count.
CellValue = Decimal | dt.date | int | str | None


class InstrumentHitOut(_Out):
    """One typeahead result — docs/07: `GET /instruments?search=…&limit=…`."""

    symbol: str
    name: str
    series: str | None


class InstrumentSearchOut(_Out):
    data: list[InstrumentHitOut]


class CellOut(_Out):
    """One number with the context that makes it mean something.

    ``percentile`` is docs/08 §"Instrument factsheet"'s improvement over the reference product:
    "a small bar behind each cell showing that value's percentile within the current universe".
    ``null`` where the value is null — an unknown number has no rank.
    """

    key: str
    label: str
    value: CellValue = None
    percentile: float | None = None


class MetricCardOut(_Out):
    """docs/01 §5 block 4 — "value, sparkline, and a subdued `Median: x` line"."""

    key: str
    label: str
    value: CellValue = None
    median: CellValue = None
    #: How many observations the median was taken over, so the card never implies more.
    observations: int


class InstrumentHeaderOut(_Out):
    """docs/01 §5 block 1 — "symbol, ₹ price, full name, `NSE: SYMBOL`, index membership chips".

    Two prices, because they answer different questions: `close_raw` is the exchange print the
    header shows, `close` is the adjusted series every factor on the page was computed from
    (CLAUDE.md house rule 6). Series, Listed On and Face Value are *not* here — docs/01 §5 puts
    them in blocks 2 and 5, and duplicating them would be two places to drift.
    """

    symbol: str
    name: str
    exchange: str
    close_raw: CellValue = None
    close: CellValue = None
    isin: str | None = None


class IndexMembershipOut(_Out):
    slug: str
    name: str


class CorporateActionOut(_Out):
    """docs/01 §5 block 11 — "type / value / ex-date table"."""

    action_type: str
    ex_date: dt.date
    ratio_from: CellValue = None
    ratio_to: CellValue = None
    amount: CellValue = None


class LabelledValueOut(_Out):
    label: str
    value: CellValue = None


class MarketQualityOut(_Out):
    """docs/01 §5 block 10, with docs/05 §15's two distances beside the label."""

    regime: str | None = None
    regime_distance_bull: float | None = None
    regime_distance_bear: float | None = None
    median_vol_12m: CellValue = None
    circuits: list[LabelledValueOut]
    positive_days: list[LabelledValueOut]


class FactsheetOut(_Out):
    """docs/07: "Factsheet payload mirrors the teardown §5 exactly"."""

    symbol: str
    as_of: dt.date
    data_version: int
    header: InstrumentHeaderOut
    key_stats: list[CellOut]
    pros: list[str]
    cons: list[str]
    #: Rules whose inputs are NULL, so the UI can explain a short list rather than implying a CON.
    undecided: list[str]
    metric_cards: list[MetricCardOut]
    price_and_mas: list[CellOut]
    returns: list[CellOut]
    sharpe_returns: list[CellOut]
    volatility: list[CellOut]
    rsi: list[CellOut]
    market_quality: MarketQualityOut
    corporate_actions: list[CorporateActionOut]
    index_memberships: list[IndexMembershipOut]
    #: The comparison set every percentile is measured against, named for the tooltip.
    percentile_universe: IndexMembershipOut | None = None
    notes: list[str] = Field(default_factory=list)


class HistoryPointOut(_Out):
    date: dt.date
    value: CellValue = None


class InstrumentHistoryOut(_Out):
    """docs/07: `GET /instruments/{symbol}/history?from&to&field=` — "sparkline series"."""

    symbol: str
    field: str
    from_: dt.date = Field(alias="from")
    to: dt.date
    points: list[HistoryPointOut]

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CorporateActionsOut(_Out):
    symbol: str
    data: list[CorporateActionOut]


class RankPointOut(_Out):
    as_of: dt.date
    rank: int
    result_count: int


class RankHistoryOut(_Out):
    """docs/07: "this stock's rank over time in a screen"."""

    symbol: str
    screen_public_id: str
    screen_name: str
    data: list[RankPointOut]


# ---------------------------------------------------------------------------
# Market data surfaces (docs/07 §"Market data surfaces", docs/01 §6 and §7)
# ---------------------------------------------------------------------------


class IndexRowOut(_Out):
    """One row of docs/01 §7's ~145-index dashboard.

    "each: name, % change, level, absolute change, PE, PB, Div Yield ... Includes derived indices
    with no fundamentals (`Nifty50 PR 1x Inverse`, `India VIX`) → PE/PB/DivYield shown as `-`."

    `null`, not `0`, for a missing fundamental — the em dash is the UI's rendering of the absence,
    and a zero would be a claim that the index has a P/E of nought.
    """

    slug: str
    name: str
    #: True for the 14 selectable universes, false for a dashboard-only index (docs/01 §7).
    is_universe: bool
    level: CellValue = None
    change_abs: CellValue = None
    change_pct: CellValue = None
    pe: CellValue = None
    pb: CellValue = None
    div_yield: CellValue = None
    #: docs/08 §Dashboard: "a sparkline per index (30-day)". Oldest first, closes only.
    sparkline: list[Decimal] = Field(default_factory=list)


class IndexDashboardOut(_Out):
    as_of: dt.date
    data_version: int
    data: list[IndexRowOut]


class GaugeOut(_Out):
    """One of docs/01 §6's four breadth gauges."""

    key: str
    label: str
    #: A percentage, 0-100, at the stored precision. `null` where the inputs are absent.
    value: CellValue = None


class MarketHealthOut(_Out):
    """docs/01 §6 — `Market Health — <universe>` and its four gauges."""

    as_of: dt.date
    data_version: int
    universe: IndexMembershipOut
    gauges: list[GaugeOut]
    constituent_count: int | None = None
    #: docs/01 §6's "Data available from 1st Nov 2024", read from the earliest stored row for this
    #: universe rather than from a constant — see `docs/11a` §3.
    data_available_from: dt.date | None = None


class MarketHealthPointOut(_Out):
    """One day of the four series, with the universe's own index level for the overlay."""

    date: dt.date
    pct_above_200dma: CellValue = None
    pct_above_50dma: CellValue = None
    pct_within_10pct_ath: CellValue = None
    pct_ret_1y_positive: CellValue = None
    constituent_count: int | None = None
    #: docs/08 §"Market Health": "a history chart of each breadth series with the Nifty overlaid".
    index_level: CellValue = None


class MarketHealthHistoryOut(_Out):
    """docs/07: `GET /market-health/history?universe=&from=&to=`.

    "The four series for charting", plus the overlay docs/08 §"Market Health" asks for.
    """

    universe: IndexMembershipOut
    from_: dt.date = Field(alias="from")
    to: dt.date
    points: list[MarketHealthPointOut]

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ListingOut(_Out):
    """One row of the listings register — an `instrument`, not a table of its own."""

    symbol: str
    name: str
    series: str | None = None
    isin: str | None = None
    listed_on: dt.date | None = None


class ListingsPage(_Out):
    """docs/07: `GET /listings?from&to&series=&cursor=` — "NSE listings, newest first"."""

    data: list[ListingOut]
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Auth and accounts (docs/07 §"Account & billing", docs/11 §Security)
# ---------------------------------------------------------------------------

#: RFC 5321 caps a path at 254 octets. Anything longer is not an address.
MAX_EMAIL_LENGTH = 254

Email = Annotated[EmailStr, Field(max_length=MAX_EMAIL_LENGTH)]
#: docs/11 leaves the length to NIST SP 800-63B: minimum eight, no composition rules.
Password = Annotated[str, Field(min_length=8, max_length=128)]
DisplayName = Annotated[str, Field(min_length=1, max_length=120)]


class RegisterIn(_In):
    """docs/07: `POST /auth/register`.

    ``password`` is optional because docs/11 §Security makes OTP "the default path, password
    optional" — an account created without one signs in by code until it sets one.
    """

    email: Email
    password: Password | None = None
    name: DisplayName | None = None
    #: docs/11 §Compliance: the DPDP consent record. Registering without agreeing is a 400.
    accept_terms: bool = False
    accept_marketing: bool = False


class LoginIn(_In):
    email: Email
    password: str = Field(min_length=1, max_length=128)


class RequestOtpIn(_In):
    email: Email


class VerifyOtpIn(_In):
    email: Email
    code: str = Field(min_length=4, max_length=10)


class ForgotPasswordIn(_In):
    email: Email


class ResetPasswordIn(_In):
    token: str = Field(min_length=8, max_length=256)
    password: Password


class ChangePasswordIn(_In):
    """``current_password`` is optional: an OTP-only account is *setting* one for the first time."""

    current_password: str | None = Field(default=None, max_length=128)
    new_password: Password


class VerifyEmailIn(_In):
    token: str = Field(min_length=8, max_length=256)


class UpdateMeIn(_In):
    name: DisplayName | None = None


class DeleteAccountIn(_In):
    """Deleting is irreversible after the window, so it asks for the address back."""

    email: Email


class SessionOut(_Out):
    """What a successful authentication returns.

    The refresh token is **not** here — it goes out as an httpOnly cookie and nowhere else
    (docs/11 §Security). A response body that also carried it would put a 30-day credential
    somewhere JavaScript can read.
    """

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    public_id: str
    email: str
    name: str | None = None
    email_verified: bool = False


class AcceptedOut(_Out):
    """The deliberately uninformative answer to "did that address exist?".

    docs/11's threat model: `/auth/request-otp` and `/auth/forgot-password` answer identically for
    a known and an unknown address, or they are a membership oracle.
    """

    status: Literal["accepted"] = "accepted"
    detail: str


class EntitlementsOut(_Out):
    """docs/07 §Entitlements, field for field."""

    screener: bool
    export_csv: bool
    custom_columns: bool
    historical_ranks: bool
    backtests: bool
    api_access: bool
    max_screens: int


class MeOut(_Out):
    """docs/07: `GET /me` -> "profile + entitlements"."""

    public_id: str
    email: str
    name: str | None = None
    email_verified: bool
    has_password: bool
    created_at: dt.datetime
    plan_code: str | None = None
    subscription_status: str | None = None
    entitlements: EntitlementsOut
    #: Set while an erasure is pending, so the UI can offer to cancel it (Prompt 12 §5).
    deletion_scheduled_for: dt.datetime | None = None


class DeletionOut(_Out):
    status: Literal["scheduled", "cancelled"]
    purge_after: dt.datetime | None = None
    detail: str


class DataExportOut(_Out):
    """docs/11 §Compliance: "DPDP Act: ... **data export** and deletion endpoints".

    Everything the account owns, in one JSON document. `screens` and `consents` are lists of
    objects rather than typed models because their shapes are already defined elsewhere and a
    second copy here would be a second thing to keep in step.
    """

    exported_at: dt.datetime
    profile: MeOut
    screens: list[JsonValue]
    consents: list[JsonValue]
    subscriptions: list[JsonValue]
    payments: list[JsonValue]


# ---------------------------------------------------------------------------
# Problems (documented so the generated client knows the error shape)
# ---------------------------------------------------------------------------


class ProblemOut(BaseModel):
    """RFC 9457. ``extra="allow"`` carries the per-type members docs/07 attaches."""

    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None


HealthState = Literal["ok", "degraded"]


class HealthOut(_Out):
    status: HealthState
    environment: str

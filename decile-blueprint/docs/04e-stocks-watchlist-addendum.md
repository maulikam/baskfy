# 04e — Addendum: the Stocks watchlist and the saved discover preferences

Two tables `docs/04-data-model.md` does not define, both added by AF lane I in migration
`0045_instrument_watch_and_prefs`: `instrument_watch_item` and `user_discover_preferences`.
Both are additions to the data model, not changes to it, and both are keyed to an account.

They are written down here because `packages/core/tests/test_schema_matches_docs.py` treats a
table nobody wrote down as a table nobody maintains — the gate that made this file necessary.

---

## 1. `instrument_watch_item`

### Why it is not `cb_watchlist_item`

The curated-basket watchlist (`cb_watchlist_item`, docs/smallcase/03) bookmarks a *basket*. The
swing book's `sw_watch` marks a *setup* the operator wants the monitor to keep raising. Neither
answers "I am following this company": starring RELIANCE from its factsheet is a bookmark on a
symbol, with no basket behind it and no strategy attached. Reusing either table would have meant
a nullable basket id or a sleeve column that is never read, and a list query that has to know
which half of the table it is looking at.

### Shape

| Column | Type | Note |
|---|---|---|
| `id` | bigint PK | |
| `user_id` | bigint → `app_user.id` ON DELETE CASCADE | the account; erasure takes the list with it |
| `instrument_id` | bigint → `instrument.id` | |
| `watched_at` | timestamptz | newest first is the list order |
| `close_at_watch` | `PRICE` numeric, nullable | the close when the name was starred |

Unique on `(user_id, instrument_id)` — starring a name twice is one row whose `watched_at` and
`close_at_watch` move forward, not a second row. Indexed on `user_id`, which is every query.

`close_at_watch` is **nullable on purpose**. "Moved since you added it" needs a baseline, and an
instrument with no bar yet has none; the column stays null and the API answers `moved_pct: null`
rather than inventing a baseline from today's close, which would report every new name as flat.
House rule 9: it is `numeric`, never float.

## 2. `user_discover_preferences`

### Why a row rather than only URL state

`/discover`'s query string stays the shareable filter — a link someone sends is the filter they
saw. The row is what onboarding writes and what a returning session reads **when there is no
query string**, so the goal composer's answers survive a new browser rather than living in one
tab's history.

### Shape

| Column | Type | Note |
|---|---|---|
| `user_id` | bigint PK → `app_user.id` ON DELETE CASCADE | one row per account; the key is the user |
| `goal`, `horizon`, `risk`, `rebalance` | text | the composer's four answers |
| `amount` | `INR` numeric | whole rupees; money is numeric, never float |
| `onboarding_completed_at` | timestamptz, nullable | set when connect → import → pick-a-basket finishes |
| `updated_at`, `created_at` | timestamptz | |

`onboarding_completed_at` being null is the honest expression of "not finished yet", which is why
completion is a timestamp rather than a boolean: the page can say when, and a re-run of the flow
cannot silently un-finish it.

## 3. Scoping

Every route over both tables (`/watchlist/instruments`, `/watchlist/discover-preferences`) calls
`scoped_sole_user_id` first, so a principal who is not this deployment's sole tenant is refused
rather than collapsed onto it (`services/api/src/baskfy_api/curated_tenant.py`). Neither table is
reachable without an authenticated user, and neither has any order path.

# Recorded NSE filings payloads (SW11B)

**Synthetic, written by hand on 2 Sep 2026 from the shape the live endpoints serve** — the test
suite makes no network call (`test_network_is_blocked.py`), so these were not captured with
`curl`. Field names, date spellings and the attachment-URL form follow the endpoints as the
filings pages call them:

- `announcements-INFY.json` — `GET /api/corporate-announcements?index=equities&symbol=INFY`.
  A JSON list; the parser reads `symbol`, `desc` (subject), `attchmntText` (NSE's one-line
  summary), `attchmntFile` (the attachment URL, sometimes with a query string), `an_dt` /
  `sort_date` / `dt` (IST, no offset). Deliberately includes: a row with **no timestamp**, a
  row with **no attachment**, a URL with a **query string**, and a summary longer than the
  headline cap.
- `event-calendar-INFY.json` — `GET /api/event-calendar?index=equities&symbol=INFY`. A JSON
  list of board meetings: `symbol`, `company`, `purpose`, `bm_desc`, `bm_date` (`%d-%b-%Y`).
  Includes an AGM row (not a result) and two result rows, out of date order.

The headline values are invented; the company names are real only to keep the shape honest.
Re-record against the live site when a run has a session that may make network calls, and
replace this note with the capture date.

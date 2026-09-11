# QUESTIONS — the handful only Maulik can answer

The charter says questions are the exception, not the rhythm. Each of these has a **default the
run builds against**, so nothing waits: if Maulik says nothing, the default is what ships, and
changing it later is a settings edit or a one-line config change, not a rebuild.

Anything needing his **hands** rather than his opinion is in `/NEEDS-MAULIK.md` § TWT instead.

---

## Q1 — The sleeve's capital

**Default the run builds against: ₹25,00,000.** Stated in the kickoff prompt of 11 Sep 2026 and
not re-litigated here.

`tw_config.sleeve_capital_inr` is **seeded at 0** and the run never sets it. A sleeve at ₹0 plans
nothing — every signal is skipped `NO_SLEEVE_CAPITAL` — so the number reaching the database is his
keystroke on the first live morning, and `FIRST-LIVE-MORNING.md` is where it happens.

One consequence worth his eye: at ₹25 lakh over ten slots a line is ₹2.5 lakh, and `04` §6.2's
1 %-of-turnover cap needs ₹2.5 crore of daily turnover before it stops binding. **No backtest in
this repository was produced at ₹25 lakh** — every number in `01` §6 was produced at ₹10 lakh,
where that cap binds on nothing. This is why the liquidity floor ships at ₹5 crore rather than the
research's ₹2 crore (`04` §3.5).

## Q2 — Equal weight, or something else

**Default: equal weight, ten slots, 10 % each**, with a 12.5 % per-position ceiling that binds only
when equity has drifted.

The research measured the alternatives at the slot count rather than at the weighting: eight slots
returns 26.3 % CAGR at −27 %, fifteen returns 14.7 % at −29 %. Concentration pays and concentration
is where the luck lives; ten is the measured middle. Risk-parity or volatility weighting was **not**
measured and this run does not invent it.

## Q3 — How many slots

**Default: 10 slots** (`tw_config.max_open_positions` = 10, ceiling `BASKFY_TWT_MAX_OPEN_POSITIONS_MAX` [15]).

It is a bounded setting rather than a constant precisely so he can move it without a deploy. Moving
it *down* concentrates the book and, on the research's own numbers, raises both the return and the
dependence on a handful of trades.

## Q4 — Half size for the first ten live entries

**Default: yes — the first ten entries taken with the flag true are sized at 0.5 × the slot.**

This is the swing pack's §3 discipline, kept because it costs almost nothing on a book that enters
about eighteen times a year. It counts **entries, not sessions** (`04` §6.4): a five-session
allowance would be spent by a quiet week without a single position being opened.

If he wants it off, it is `tw_config.first_live_entries_left = 0` — a settings write, audited in
`tw_config_audit`, no code change.

---

## What this file is not asking

* **Whether to go live without a paper phase.** Answered 11 Sep 2026: no paper phase. `02` §3 is
  written to that decision and the run does not re-open it.
* **When to flip the flag.** His, and only his (`02` §3.5). The run never sets it.
* **Whether the strategy works.** `01` §8 is the honest answer: 164 trades, about fifteen
  independent observations, and a wide confidence interval. That is on the page, not in a question.

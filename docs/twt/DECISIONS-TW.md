# DECISIONS-TW — judgement calls of the TW run

Same convention as `docs/DECISIONS-MERGE.md`, `docs/swing/DECISIONS-SW.md`,
`docs/vbt/DECISIONS-VB.md` and `docs/condor/DECISIONS-OC.md`: numbered by module; the context, the
choice taken, the rejected alternatives and why, and the reversal path. Decisions taken without
Maulik's review are tagged **⚠ UNREVIEWED** until he clears them.

The charter's instruction is decide-record-continue. **TW0 pre-takes eight** so no later module
stalls on a question the pack can already answer, and each is cheap to reverse.

---

## TW0.1 — The look-ahead reading is not a configuration of the sleeve · ⚠ UNREVIEWED

**Context.** `research/tight-close/STRATEGY.md` §1 is the run's most load-bearing finding:
Chartink's *backtester* evaluates a weekly candle as a completed candle, so on a Tuesday it already
knows Friday's close. Read that way the panel reproduces 83.1 % of Chartink's stock-days at 97.8 %
precision; read point-in-time — today's close as the current week's close — it reproduces 64.9 % at
61.5 %. The second is the scan a person can actually run at 15:30, and it is also what Chartink's
*live* scan shows.

**Taken.** `baskfy_core.twt.signals.tight_state` implements the point-in-time reading **and takes no
switch**. There is no `include_current_week` parameter, no `lookahead=` keyword and no
`TightReading` enum. TW2 needs the look-ahead number to prove the gap is Chartink's candle
semantics rather than a plant defect, so `tight_state_lookahead` lives in the **test module**
(`packages/core/tests/twt_lookahead.py`) and `test_twt_lookahead_recall.py` asserts by import scan
that `baskfy_core.twt` cannot reach it.

**Rejected.** (a) A config field defaulting to point-in-time — a field that exists is a field
somebody turns on, and the one that would be turned on here makes every stored signal a number the
market never offered. (b) Dropping the look-ahead reading entirely — then `01` §2's 83.1 % is an
unverifiable claim in a document, and the next session to notice a 65 % recall will read it as a
bug in the plant and go looking for bars that are not missing.

**Reversal.** Move one function from the test module into the package and add a parameter. Nothing
stored changes shape. Doing it would need a `DECISIONS-TW` entry saying why the sleeve should trade
a signal that is not visible at the close.

## TW0.2 — The rank key is the signal session's own turnover, not the 20-day average · ⚠ UNREVIEWED

**Context.** `research/tight-close/STRATEGY.md` §3 says candidates are "ranked by 20-day turnover
when there are more signals than slots". The code that produced **every number in that note**
ranks by `ind.turnover` — the signal day's own `close_raw × volume` (`research/volume-breakout/vbt/sim.py`,
`key["turnover"]`). The prose and the measured strategy disagree.

**Taken.** The sleeve ranks by the signal session's own turnover, `EntryConfig.rank_key =
SIGNAL_TURNOVER`, stored as `tw_signal_daily.rank_key`. This is the root `CLAUDE.md` rule of
9 Sep 2026 applied to a research note instead of a setting: **the doc describes intent, the code
that produced the numbers is the later fact, so fix the stale half.** TW0 adds one correcting line
to `research/tight-close/STRATEGY.md` §3 citing the code rather than leaving the two disagreeing.

**Why it matters and why it is small.** The ranking only binds when more than three signals clear
the gate and the liquidity floor on one session, which the research's own ablation says is worth
about 4 CAGR points against ranking by nothing. Day turnover and 20-day average turnover correlate
strongly on names that clear a ₹5 crore floor, so the two orderings rarely differ — but *rarely* is
not *never*, and TW2's goldens are a tick comparison that the wrong key would break.

**Rejected.** (a) Ranking by the 20-day average because the prose says so — that pins a value to a
document, which `CLAUDE.md` forbids by name and which cost two days in September. (b) Making it a
setting — the measured differences between rank keys are a strategy change, not a preference.

**Reversal.** One enum member and one column read. `tw_signal_daily` already stores
`turnover_avg_20` beside `rank_key`, so a re-ranking needs no migration and no re-detection.

## TW0.3 — The liquidity floor ships at ₹5 crore, and the research's ₹2 crore is kept as one named field · ⚠ UNREVIEWED

**Context.** The research's headline used a ₹2 crore floor on 20-session average turnover, at a
₹10 lakh book. This sleeve will run at ₹25 lakh. Ten slots at ₹25 lakh is a ₹2.5 lakh line, and
`04` §6.2's 1 %-of-turnover cap does not stop binding until the name turns over ₹2.5 crore a day. A
floor **below** the cap means the plan is routinely sized by the cap rather than by the strategy —
the book would be systematically under-sized in exactly the names the floor was meant to admit.

**Taken.** `EntryConfig.min_turnover_inr = 50 000 000` (₹5 crore). It is the coherent floor at this
capital and it is also the better of the two in the research's own sensitivity table: 22.5 % CAGR at
−27 % on 169 trades, against 20.9 % at −24.7 % on 164.

`EntryConfig.research_min_turnover_inr = 20 000 000` exists for **exactly one caller**, TW2's golden
parameter set, because a golden that cannot reproduce the study is not a golden.
`test_twt_no_literals.py` asserts its only reference outside `config.py` is in the goldens — never
the detector, never the plan, never a page.

**Rejected.** (a) Shipping ₹2 crore for fidelity to the note — fidelity to a number measured at a
quarter of the capital is not fidelity. (b) Deriving the floor from `sleeve_capital_inr` at runtime
(`floor = capital / slots / cap`) — elegant, and it makes the universe move when a settings field
moves, which means two sessions are not comparable and the stored funnel stops meaning one thing.
(c) Deleting the research value — then TW2 has nothing to reproduce.

**Reversal.** One field in `config.py`. The stored rows carry `turnover_avg_20`, so any past session
can be re-funnelled at a different floor without re-detection.

## TW0.4 — TWT stores its own breadth row and calls VBT-1's arithmetic · ⚠ UNREVIEWED

**Context.** VBT-1's gate is the same measurement over the same universe at the same threshold: the
share of the traded universe above its own 200-day average, gate open above 40 %. Two
implementations of one measurement is the single thing that can make two pages disagree about the
same day. But a sleeve whose gate lives in another sleeve's table stops having a gate the day that
sleeve's nightly flag is set false.

**Taken.** Split the two questions. **One arithmetic:** `baskfy_core.twt.breadth` calls
`baskfy_core.vbt.breadth.breadth_series` / `breadth_above_dma`, passing a config whose breadth
section is TWT's own `BreadthConfig(dma_bars=200, min_pct_above_dma=40.0)` — **spelled out, not
inherited**, so a VBT recalibration cannot silently move this sleeve's gate, and
`test_twt_docs_parity.py` pins both numbers literally. **Two rows:** `tw_breadth_daily` is its own
table, written by its own job in its own transaction.

**Rejected.** (a) Reading `vb_breadth_daily` — a cross-sleeve runtime dependency that `02` Track C
§5 would not allow, and one `BASKFY_VBT_NIGHTLY_ENABLED=false` away from a silently SHUT gate.
(b) Reading `market_health_daily.pct_above_200dma` — that is computed over an **index's**
point-in-time membership, a different measurement of a different population, and it would change the
gate that produced every number in `01` §6 (VB0.3's finding, inherited). (c) Copying the
arithmetic into `twt/breadth.py` — two implementations, one of which will be fixed.

**Cost.** One small table, about 2,200 rows a decade. **Reversal.** Drop the table and read the
neighbour's; nothing else changes.

## TW0.5 — `trail_pct` is bounded below, not above · ⚠ UNREVIEWED

**Context.** Every other bounded setting in this repository is capped **above**, because the risk
being managed is somebody making a position bigger than the book can carry.

**Taken.** `BASKFY_TWT_TRAIL_PCT_MIN = 18.00`, a **floor**. The trail is the only exit in TWT-1
(137 of the research's 164 exits) and the measured cliff is in the tightening direction: 20 % → 15 %
took the CAGR from 20.9 % to 9.6 % and the drawdown from −24.7 % to −43 %. Widening it is merely
unprofitable (30 % → 15.5 % on 73 trades); tightening it is the failure mode. The ceiling that
matters here is a floor.

**Rejected.** (a) A ceiling as well, for symmetry — 25 % is inside the measured plateau and 30 % is
a thinner book, not a broken one; a ceiling would refuse a setting that is merely conservative.
(b) Making `trail_pct` a constant — then a bad quarter is a code change under pressure, which is
worse than a bounded form.

**Reversal.** One env default, and `tw_config_audit` carries the history of every write either way.

## TW0.6 — A fresh listing has no entry event · ⚠ UNREVIEWED

**Context.** `04` §3.4's entry event is "the state is true today and was false on each of the
previous five sessions". The research's implementation seeds its sessions-out counter at a large
number, so a name whose **first ever bar** satisfies the scan fires an entry on its listing day:
"was false for five sessions" is read as true because there were no five sessions to be true in.

**Taken.** `entry_events` requires `entry_min_sessions_out` [5] sessions of **existing** history
before *t*. A claim about five sessions is a claim about five sessions that exist. TW2 measures
how many of the research's 164 trades this moves and records the number here; if it moves any, the
goldens assert the explained delta rather than the raw list.

**Rejected.** Reproducing the research's seeding so the goldens match exactly. The goldens exist to
prove the arithmetic, not to inherit a bug: a listing-day signal has no base to be tight in, and
three weekly closes of which two do not exist is not a 3 % range.

**Reversal.** One comparison in `entry_events`. Stored `sessions_out_before` makes any past session
re-readable under either rule.

## TW0.7 — A corporate action may raise a stop or alert, never lower one · ⚠ UNREVIEWED

**Context.** `high_since` and `gtt_trigger` are exchange prices; the bar series underneath them is
adjusted. A split during a 600-session hold — ordinary on this book — rewrites the adjusted series
and leaves the resting GTT at the exchange quoting a pre-split price on a post-split instrument.
Arithmetically, re-deriving the trigger from the newly adjusted highs produces a **lower** number.

**Taken.** The evening job detects the change (the as-of row's `adj_factor` differs from the
position's `entry_adj_factor`), re-derives `high_since` from the adjusted high series scaled by the
new factor, and then:

* if the re-derived trigger is **above** the resting one, it writes `next_trigger` and the morning
  carries a `RAISE_GTT_STOP` line whose reason names the adjustment;
* if it is **below** — which is what a split does — **no line is emitted** and
  `TWT_ADJUSTMENT_RESET` is raised instead, because a person has to look at the resting order.

**Why.** Cancelling a resting stop and arming a lower one is the one thing this sleeve must never do
on its own, and a split is the one event that would make doing it look correct. The failure is
silent: a stop that quietly fell is a stop nobody notices until it does not fire.

**Rejected.** (a) Re-arming at the arithmetically correct post-split level automatically — correct
and still forbidden, because the code cannot tell a genuine 1:2 split from a bad `adj_factor`, and
the plant's pre-2024 corporate actions are sparse (403 rows, 2024 →). (b) Leaving the stale GTT
untouched and silent — the position is then protected at a level that means nothing.

**Reversal.** None wanted. If Maulik decides the automatic re-arm is right, it is a module with its
own gates, not an edit.

## TW0.8 — TWT's stop band is 0.5–30 %, additive, and the desk's own band is untouched · ⚠ UNREVIEWED

**Context.** The desk's `StopBand(0.08, 0.12)` encodes the **weekly book's** vol-scaled 8–12 % stops
(non-negotiable 4). The swing sleeve already carries its own `StopBand(0.005, 0.10)` (SW PACK.3) and
VBT-1 its own `StopBand(0.005, 0.15)` (VB PACK.3). A 20 % stop is outside all three, and a band that
refuses this sleeve's own stop makes non-negotiable 4 unsatisfiable — there would be no legal way to
arm the GTT that the non-negotiable requires.

**Taken.** `TWT_STOP_BAND = StopBand(min_pct=0.005, max_pct=0.30)`, an additive constant on the TWT
route only, with `TWT_GTT_LIMIT_FRACTION = 0.97` as the cushion keyword to `place_gtt_stop`. The
ceiling is 0.30 rather than 0.25 because `tw_config.stop_pct`'s own ceiling is 25 % and a band equal
to the setting's ceiling would refuse the ceiling itself on a tick-floor rounding.

**Rejected.** Widening the desk's default band — that silently changes the weekly book's stops,
which Track C §8 forbids by name, and `BASKFY_TWT_*` ceilings would stop meaning anything.

**Reversal.** Two constants in the desk's TWT route. The weekly book never sees either.

## TW3.1 — `models/twt.py` spells its vocabulary out instead of importing TW1's enums · ⚠ UNREVIEWED

**Context.** `models/swing.py` and `models/vbt.py` import every state, kind and reason from their
engine package, so the database's CHECK constraints and the code that writes them cannot disagree:
one definition, two consumers. TW3 could not do that. `baskfy_core.twt` is **TW1's**, it did not
exist when TW3 began, and TW1 was being authored in a parallel session that TW3's brief forbids
touching. A module-scope import of a package another session is mid-way through creating makes
this schema unimportable for as long as that session runs — and `baskfy_core.models` is imported
by alembic's `env.py`, so the whole migration chain would have gone with it.

**Taken.** The `TW_*` tuples are transcribed into `packages/core/src/baskfy_core/models/twt.py`
from `docs/twt/03` §§3-8 and `04` §10.1, and **pinned to those documents** by
`packages/core/tests/test_twt_schema.py::TestTheVocabularyIsTheDocuments` — every value, plus the
absences that matter (`EMA_EXIT` and `TIME_EXIT` are not close reasons; `EXPIRED` is not an order
state). The document is the specification both halves are written against, so pinning to it is
not pinning to the code's current behaviour (house rule 2).

**Rejected.** (a) A lazy import inside a function — house rule 3 forbids the escape hatch and
mypy --strict would not type it. (b) Waiting for TW1 — TW4 needs the tables, and a schema module
that blocks on a sibling is a serialised run. (c) A `try: import ... except ImportError:` fallback
— a silently swallowed exception, and the failure mode is a constraint list that quietly differs
from the engine's.

**Reversal.** Replace each tuple with `tuple(x.value for x in <Enum>)` importing from
`baskfy_core.twt.config` / `.plan` / `.exits`, and keep the document test beside it — two
consumers of one definition, and the document still the referee. Nothing stored changes shape;
`TestTheVocabularyIsTheDocuments` is the test that proves the swap changed nothing. **TW4 is the
natural place to do it**, since it is the first module that imports both halves.

## TW3.2 — `tw_position.entry_adj_factor` exists although `03` §5 does not list it · ⚠ UNREVIEWED

**Context.** `04` §7.3 and TW0.7 describe the corporate-action branch of the evening job in these
words: *"the evening job detects it (the as-of row's factor differs from the position's
`entry_adj_factor`)"*. `03` §5's column table does not contain that column. The two halves of the
specification disagree by one field, and the field is the input to the one rule this sleeve must
never get wrong — a split during a 600-session hold is ordinary, and re-deriving a stop from the
newly adjusted series produces a **lower** number.

**Taken.** The column is created: `entry_adj_factor ADJ_FACTOR NOT NULL DEFAULT 1`. `03` §5's
table is a summary and `04` §7.3 is the rule; a rule that names a column by its field name is a
later, more specific fact than a table that omits it, and the cost of being wrong in this
direction is one unused column, while the cost of being wrong in the other is a migration on the
morning of a split, written under pressure, against a live book.
`test_schema_matches_docs.py::test_twt_position_carries_the_factor_its_split_rule_reads` records
which document asked for it.

**Rejected.** (a) Leaving it out and letting TW7 add it — that is a migration during the module
that has to handle a split correctly, which is exactly the wrong moment. (b) Recomputing the
entry-day factor from `ohlcv_daily` — the adjusted series is what a corporate action *rewrites*,
so the number the position was actually sized against stops being recoverable the moment it
matters. `03` §5's own argument for storing `high_since` is the same argument.

**Reversal.** Drop the column in a later migration; nothing else reads it yet. The honest
alternative is to add the one row to `03` §5's table, which TW4 should do when it writes the
field for the first time.

## TW3.3 — a settings write below a floor gets its own problem type · ⚠ UNREVIEWED

**Context.** TW0.5 makes `trail_pct` the one bounded setting in this repository whose bound is a
**minimum**. `baskfy_api.problems` had exactly one refusal for a bounded setting,
`setting-above-ceiling`, whose title is *"Setting exceeds the server's ceiling"*.

**Taken.** `ProblemType.SETTING_BELOW_FLOOR` ("setting-below-floor", 422, *"Setting is below the
server's floor"*) and `problems.setting_below_floor(...)`, carrying `field`, `requested`, `floor`
and `env_var` — the mirror of the ceiling's refusal, which carries `ceiling`. Additive: no
existing type, status, title or caller changes, no route is added, and `422` was already in the
OpenAPI status set that `test_api_artifacts.py` checks, so the generated client is unaffected.

**Rejected.** (a) Reusing `setting-above-ceiling` with a re-worded detail — a refusal *typed*
"above ceiling" for a value that was too **small** is precisely the confusion TW0.5 exists to
prevent, and it is the form in which a future reader would "fix" the floor into a ceiling.
(b) A 400 — the payload is well formed and the value is a number a person can legitimately want;
it is the M4.1 boundary refusing, which is a 422 for the same reason the ceiling's is.

**The order of the enum member is load-bearing, and this is how we found out.**
`app._DOCUMENTED_ERRORS` builds one OpenAPI response per **status**, from a dict comprehension
over `STATUS_FOR` — so several problem types sharing 422 collapse to whichever is declared *last*.
(That is already why 422 is described as the ceiling's refusal rather than `NO_TRADING_DAY`'s.)
Declared after `SETTING_ABOVE_CEILING`, the new type rewrote the 422 description on **every route**
in `packages/api-client/openapi.json` and therefore in the generated TypeScript client;
`test_api_artifacts.py::test_openapi_json_is_current` caught it. `SETTING_BELOW_FLOOR` is
therefore declared **before** `SETTING_ABOVE_CEILING` in the enum, in `STATUS_FOR` and in
`TITLE_FOR`, the published document is byte-identical to what was committed, and
`test_twt_ceilings.py::test_adding_the_floors_type_did_not_rewrite_every_422_in_the_contract`
pins it. A comment at the enum member says the same thing for whoever adds the next 422.

**Reversal.** Delete the enum member, the status/title rows and the helper, and point
`FLOOR_ENV`'s refusal at `setting_above_ceiling`. One file.

## TW8.1 — the desk page ships as a shape in `apps/web`, mounted on no web route · ⚠ UNREVIEWED

**Context.** `05` §2's desk page is the operator surface where a plan line becomes an order, and
the desk console is a Jinja application in `kite-momentum-rebalancer`. TW8's gate checks all run
in `apps/web`, and TW8's own brief scopes the module to `apps/web` and repeats the product's first
non-negotiable: *"The web app has no order route and does not gain one."* Those three facts cannot
all be satisfied by putting a Confirm button on a web route.

**Taken.** The desk page is built as `apps/web/src/components/twt/desk/desk-plan.tsx` — the page's
**shape**: the section order (stops, then entries, then the open positions), the mode badge, the
expiry rule, the 15:15 band, the skip reasons in words. It is pure, its clock is a parameter, and
its confirm control is a **slot the host supplies**. `apps/web` supplies none and mounts it on no
route; `read-only.test.tsx` asserts that no page under `(app)/twt` so much as names it. Every rule
of `05` §2 is therefore asserted by a test today, and TW6 wires a real confirm behind it in the
desk console, where law 2 says an order is created.

**Rejected.** (a) A `/twt/desk` route in the web app with a real Confirm — breaches non-negotiable
1, law 2 and Track C §4, and no gate wording outranks those (charter precedence 1 and 2 over 5).
(b) Writing `kite-momentum-rebalancer/app/templates/twt.html` — the right long-term home, and
outside this module's ownership; it also needs TW6's `/twt/execute` route, which does not exist.
(c) Building nothing and recording the gate as blocked — the expiry rule and the section order are
the two things on that page most worth a failing test, and they are testable today.

**Reversal.** TW6 either renders this component from a desk-side surface or transcribes it into
the Jinja template and deletes it. Nothing else depends on it.

## TW8.2 — the caveats keep every number verbatim and drop the word "repository" · ⚠ UNREVIEWED

**Context.** `05` §3 requires `01` §8's caveats on the page as a component, and names the sentence
"**no backtest in this repository was produced at ₹25 lakh**". The portfolio work of 11 Sep 2026
established the other rule that binds this page: nothing from the inside of the system reaches a
reader's eyes.

**Taken.** Every **number and quantity** is transcribed exactly — 164 trades, ten of them 53 % of
gross profit, roughly fifteen independent observations, the 20 % give-back as routine, ₹50,000 on
a ₹2.5 lakh line, ₹10 lakh, ₹25 lakh. The one phrase not transcribed is "in this repository",
which is a fact about where code is kept; the sentence rendered is *"One thing none of these runs
has measured: this strategy at ₹25 lakh"*, which says the same thing about the same set of runs.
`backtest.test.tsx` pins each number.

**Rejected.** Rendering the phrase as written — it puts an engineering word on a page a person
reads before committing money, which is the defect the portfolio screenshot found.

**Reversal.** One string in `components/twt/caveats.tsx` and one assertion.

## TW8.3 — the stored signal tags are rendered as words, not as tags · ⚠ UNREVIEWED

**Context.** `05` §1.2 describes the entry badge as `SIGNAL`, `SCAN_ONLY (turnover)`, or nothing.
Those are `tw_signal_daily.state` values and `failed_filters` entries — the spellings the database
uses.

**Taken.** The badge reads **"Entry today"**; a rejected event is shown in a disclosure carrying
its own count, badged **"Watch only"**, with the reason in a reader's words ("not enough trades
through it on an average day to buy and sell without moving the price"). Nothing is hidden —
`05` §1.2's requirement that the rejects be visible and carry their reason is met in full — and
`no-internals.test.tsx` asserts the tag itself never reaches the screen. This is the same
substitution `DECISIONS-VB` VB8.5 made for "Book" → "Positions", for the same reason.

**Rejected.** Printing the tags. A screen that shows what it rejected is auditable; a screen that
shows it in a spelling only the code understands is not.

**Reversal.** Two constants in `lib/twt/copy.ts`.

## TW8.4 — the payload names its units, and a rate converts exactly once · ⚠ UNREVIEWED

**Context.** TW4 and TW5 have not landed, so TW8 defines the shape of what it reads. On 11 Sep
2026 the portfolio band rendered a 1.99 % day as 0.0199 % because a component scaled a figure the
builder had already scaled.

**Taken.** Money and prices are **decimal strings** everywhere (house rule 9). Rates carry their
unit in the field name: `*_pct` is a **percentage, as `03` stores it** (house rule 8 — storage
precision is the contract), and `*_fraction` is a **fraction**, the convention the portfolio
payload uses. `asPercent` in `lib/twt/numbers.ts` is the only multiplication by a hundred in the
tree, and `numbers.test.ts` pins `0.019900 → 1.99`.

**Rejected.** Serving every rate as a fraction — it would make the API restate figures `03`
already rounded at write time, and re-rounding a stored percentage is house rule 8 in reverse.

**Reversal.** If TW5 serves fractions for the stored columns instead, the field names change and
the view model gains one `asPercent` call per field. The unit is in the name, so the change is
mechanical and a wrong one fails the test above.

## TW8.5 — `copy.ts` lives in `lib/twt`, not beside the page · ⚠ UNREVIEWED

**Context.** `05` §1 says the copy "lives in `copy.ts` beside `/vbt`'s", i.e. inside the route
tree. `05` §2's operator view says versions of the same sentences about the same gate.

**Taken.** One module, `lib/twt/copy.ts`, imported by the pages and by the components. A copy
file only one of the two surfaces can import is exactly the drift a copy file exists to prevent.

**Reversal.** Move the file and update six imports.

## TW8.6 — the hub is two tabs, and the open positions live on the hub · ⚠ UNREVIEWED

**Context.** The sibling sleeve's hub is three tabs (Today | Positions | Backtest). `05` §1 gives
this one three cards on **one** page — the gate, today's names, the open positions — and §3 gives
it a backtest page.

**Taken.** `SECTION_TABS.twt` is "Today | Backtest". This strategy holds about ten lines for about
a year each; a second page to hold ten rows is a click in front of the thing the reader came for.
`nav.test.ts` asserts both tabs and that each has a page record.

**Reversal.** One array in `lib/nav.ts` and a route directory.

## TW8.7 — the hub has no server action at all, not even the two money-free ones · ⚠ UNREVIEWED

**Context.** `05` §1 permits exactly two mutations from this hub — a note and a dismissal —
because they change no money.

**Taken.** Neither is built. `03` has no table to put a note in, and inventing one to hold an
affordance nobody has asked for would be a migration in service of a mock-up — the sibling sleeve
reached the same conclusion (DECISIONS-VB VB8.4). So `read-only.test.tsx` asserts the stronger
claim that is available: no `use server` module, no form, no route handler, no order-shaped verb
anywhere under the tree, and therefore every method but GET is Next's own 405.

**Reversal.** A table in a later migration, then a server action and a narrowed assertion — never
a widened one.

## TW2.1 — The golden panel is the study's own `panel.pkl`, read without importing the research · ⚠ UNREVIEWED

**Context.** TW2 has to run the sleeve over *the bars the study ran on*, and those are
`research/volume-breakout/data/panel.pkl` — 4,186 instruments x 2,396 sessions, 3,578,815 bars,
2017-01-02 → 2026-09-09. It is 1.7 GB and gitignored. VBT-1's harness took the other road and
loads the **CSV export** (`research/volume-breakout/aws/`), re-deriving `04` §1's universe itself.

**Taken.** `tools/twt/twt_panel.py` reads the pickle and converts it to the frame
`baskfy_core.twt.indicators` requires. The research package is **never imported**: `panel.pkl`
pickles two research dataclasses (`vbt.data.Panel`, `vbt.scan.Indicators`) and a remapping
unpickler rebinds them to plain carriers, refusing every module outside numpy and the builtins. So
loading the study's panel cannot execute `research/volume-breakout/vbt/`, cannot put it on the
import path, and cannot write anything.

**Why the pickle rather than the CSVs.** TW2's job is to reproduce *this study's* numbers, and the
study ran on the pickle. The pickle already has the six thin sessions of `04` §2.1 removed (the
research dropped them when it built it), which is itself a cross-check: TWT's own thin-session rule
run over the loaded frame finds **nothing left to drop**, and the six dates are absent. Two
implementations of §2.1, one answer.

**The one column the pickle does not carry: `adj_factor`.** The research panel stores `close`
(adjusted) and `close_raw` (the exchange print) and no factor. `with_twt_indicators` fills the
missing column with 1, which is right for a backtest — nothing in `04` §3-§8 reads `adj_factor`;
only TW4's stored levels and TW7's corporate-action branch do, and neither is in the golden path.
A TW9 run over the plant's bars gets the real factor.

**Rejected.** (a) Loading the CSV export as VBT-1 does — it would reproduce a *re-derivation* of
the study's universe rather than the study's own, and the first difference found would be
unattributable. (b) `sys.path.insert` + a plain `pickle.load` — three lines shorter and it executes
research code to do it.

**Reversal.** One module, no callers outside `tools/twt/`.

## TW2.2 — ETFs stay as rows in the golden panel, and the difference it makes is measured at zero · ⚠ UNREVIEWED

**Context.** The study kept ETFs as rows in the panel and excluded them *inside* the scan
(`~is_etf`), which means its **breadth denominator counted them**. `04` §1.3 drops them from the
universe, so the plant's denominator is smaller. A reproduction has to choose, and choosing
silently is how a difference becomes invisible.

**Taken.** The loader keeps them (`keep_etf_rows=True`, the research's reading) and takes a keyword
to drop them, so the difference is *measured* rather than assumed. Measured: the 349 ETF
instruments carry **2,786 bars in the whole panel** — 0.08 % of 3,578,815, and none at all on a
typical session, because the export's `ohlcv_daily` barely covers them. Breadth is **identical to
every decimal place** under both readings, and so is every gate verdict on all 2,396 sessions.

**Why it still matters to have looked.** The number is zero *on this panel*. On the plant's bars,
where ETFs do print daily, it would not be, and TW9 must not inherit an assumption that was only
ever true of a sparse export.

**Reversal.** A keyword argument. The state count is unaffected either way (115,551 both ways):
`signals.tight_state` excludes ETFs itself.

## TW2.3 — The look-ahead reading is not in the research's code; it was rebuilt from STRATEGY §1's sentence · ⚠ UNREVIEWED

**Context.** `01` §2 and `research/tight-close/STRATEGY.md` §1 report **83.1 % recall at 97.8 %
precision** for the look-ahead reading, and that number is the whole argument that the gap between
the sleeve and Chartink's export is Chartink's *candle semantics* rather than a defect in the
plant. TW2 has to reproduce it. **It is not computed anywhere in `research/`.** `tscan.py` has an
`include_current_week` switch, but `False` there means *the three completed weeks before this one*
— a lag, not a look-ahead — and `tscan_verify.py` scores only that switch crossed with
`months_ago ∈ {2, 3, 4}`. Neither produces 83.1 %.

**Taken.** The reading was rebuilt from the note's prose — "the current week's *final* close used
on every day of that week" — as `w0 = the last close of the current week bucket`, `w1`/`w2`
unchanged. It reproduces **83.1 % / 97.8 %** exactly, against the point-in-time reading's
**64.9 % / 61.5 %**, both to the decimal place the note prints.

**Why that is evidence and not a coincidence.** Five further published numbers fall out of the same
re-implementation without being aimed at: 26,767 entry events, 21,378 of them above ₹2 crore, 9,254
Chartink stock-days in the window, and the two miss buckets STRATEGY §1 names by size — **832
no-bar days and 602 names without a clean 50-session volume window**. A reading reconstructed
wrongly does not hit six independent numbers.

**Where it lives.** `packages/core/tests/twt_lookahead.py`, exactly where TW0.1 put it, reusing the
sleeve's own `tight_state` and `month_low_back` so that the *only* difference between the two
readings is the one column it rewrites. `test_twt_lookahead_recall.py` asserts by AST scan that no
module of `baskfy_core.twt` names it.

**Reversal.** None wanted. If the research is ever re-run with the look-ahead reading in
`tscan.py`, this file's numbers are the thing to check it against.

## TW2.4 — The goldens cannot tell a trailing stop from a disaster stop, and TW2 must not invent the distinction · ⚠ UNREVIEWED

**Context.** `01` §5 and STRATEGY §3 say the trail is the exit — "137 of 164 trades". But the
research simulator *raises `pos.stop`* as the trail ratchets and reports every exit through it as
`stop`. `out/final_trades.csv` therefore carries five labels — `stop` (137), `stop_gap` (15),
`stop_day0` (2), `end` (10) — and **no label for the trail at all**. The 137 is the count of
`stop` rows, not a field of the file.

**Taken.** `RESEARCH_EXIT_REASONS` maps the study's five onto
`baskfy_core.twt.exits.ExitReason` one-for-one (`stop → STOP_HIT`, `stop_day0 → STOP_DAY0`,
`stop_gap → STOP_GAP`, `no_bar → NO_BAR`, `end → END_OF_RUN`) and the comparison asserts the
mapped label. **A trail-versus-disaster breakdown is not available from the goldens and TW2 does
not produce one.** If the sleeve's engine ever emits a distinct `TRAIL_HIT`, the goldens cannot
grade it and the comparison would report 137 differences that are not differences.

**Why this is recorded rather than solved.** The honest fix is a re-run of `final_tc.py` with a
distinguishing label, and `research/` is read-only. The absence is a limit on what TW2 can prove,
and a limit that is written down is not a footnote.

**Reversal.** One mapping in `tools/twt/twt_compare.py`.

## TW2.5 — Breadth agrees with the study to 0.096 of a point and on every gate verdict · ⚠ UNREVIEWED

**Context.** The gate decides whether the book may enter at all, so a breadth series that disagreed
with the study's would move trades rather than decimals.

**Measured.** Over all 2,396 sessions: the largest disagreement is **0.0957 of a percentage
point** — about one name in a thousand — the mean is 0.0017, and the number of sessions on which
the 40 % verdict differs is **zero**. The cause is the one DECISIONS-VB **VB2.2** recorded for the
same comparison: a 200-session rolling mean computed by Polars and by pandas can disagree in the
last bit, and "is the close above its own average" then has two defensible answers.

**Taken.** The test asserts the **verdict** on every session and bounds the percentage at 0.1 of a
point. Asserting the percentage to the bit would be asserting a float-summation order, which is not
the spec (house rule 2).

**Reversal.** None needed; if the gap ever widens, the verdict assertion fails first, which is the
one that matters.

## TW2.6 — The harness modules carry a `twt_` prefix, because `tools/` is one flat namespace · ⚠ UNREVIEWED

**Context.** `06` TW2 names the runner `tools/twt/goldens.py`, and the natural name for the loader
beside it is `research_panel.py` — which is exactly what `tools/vbt/research_panel.py` is already
called. Both directories are put on `sys.path` by their own test module, so in a full `pytest`
run whichever test imported first would own the name `research_panel` and the other would get the
wrong module. `mypy` resolved it the same way and said so.

**Taken.** The five modules are `twt_panel.py`, `twt_scan.py`, `twt_recall.py`, `twt_compare.py`
and `twt_goldens.py`. The plan's literal filename is the lowest-precedence criterion in the
charter's order, and the Goal it stands for — a runner that reproduces the study — is untouched.

**Rejected.** (a) Making `tools/twt` a package and importing `twt.goldens` — it keeps the plan's
names, and costs every operator invocation a `PYTHONPATH`, plus it turns every sibling directory
under `tools/` into an importable namespace package. (b) Loading the modules by path with
`importlib` as `test_swing_goldens.py` does for one file — five modules that import each other do
not survive it, and mypy cannot follow it without an escape hatch (house rule 3).

**Reversal.** Five renames and the `mypy_path` entry.

## TW2.7 — TW0.6's predicted difference measures **zero** on the study's panel · ⚠ UNREVIEWED

**Context.** TW0.6 changed the entry rule: the research seeds its sessions-out counter at 10,000
and so fires an entry on a name's **listing day**, while `04` §3.4 requires
`entry_min_sessions_out` [5] sessions of existing history first. TW0.6 said TW2 would measure how
many trades it moved.

**Measured, over the whole 2017 → 2026 history.** The sleeve's `entry_event` column and a faithful
re-implementation of the research's `entries()` produce the **same 26,767 events**, stock-day for
stock-day — not merely the same count — and the same **21,378** above the study's ₹2 crore floor.
The state itself matches at **115,551** cells. **The fresh-listing rule moves nothing on this
panel**, because a name's first bar cannot have three weekly closes to be tight in.

**Taken.** TW0.6 stands as written, and the note that it might move "the two trades in the goldens"
is now answered: it moves none. The assertion lives in `test_twt_goldens.py` so a future change to
either rule fails there.

**Caveat.** Zero *on this panel*. A plant run whose universe admits a name mid-history — a
re-listing, a symbol change — could still differ, which is why the counter is stored as
`sessions_out_before` rather than inferred.

## TW2.8 — What the recall measurement counts, and what a missing bar does to it · ⚠ UNREVIEWED

**Context.** A recall number is only as honest as its definitions, and the tempting definition is
the flattering one: drop the stock-days nobody could have produced — a name with no bar that
session, a name the panel does not carry — and recall rises from 64.9 % to something nicer.

**Taken, and asserted as tests.** A stock-day is `(session, symbol)` and nothing else. Recall and
precision are each other's mirror under swapped arguments. Duplicates collapse. An empty answer key
scores **0**, never 100. And **an unreachable stock-day is a miss and stays in the denominator**:
`MissReason` *explains* the misses — `no_bar`, `no_volume_average`, `not_tight`,
`not_above_month_low`, `below_a_floor`, `unexplained` — and nothing in the scorer can *remove*
one. The classification is asserted to sum to the miss count exactly.

**The fixtures.** The two readings over Chartink's own window are committed as gzipped stock-day
lists (95 KB in total) with a manifest carrying the window, the counts, the miss reasons and a
SHA-256 of each, so the recall test needs neither the 1.7 GB panel nor a running engine.
`uv run python ../tools/twt/twt_scan.py --verify` rebuilds all four from the panel and asserts byte
identity; a panel-gated test runs it. The **goldens** — the 164-trade list and the metrics — are
byte-for-byte copies of `research/tight-close/out/`, never regenerations, and a test proves the
copy whenever `research/` is in the checkout.

**Rejected.** Scoring against a reading this repository computed for itself, with no committed
answer key — the test would then only prove the code agrees with itself, which is the failure mode
a golden exists to prevent.

**Reversal.** Delete the fixtures and re-run `--write`.

## TW1.1 — The research's own tick is a named constant, like the research's own liquidity floor · ⚠ UNREVIEWED

**Context.** `04` §7.1 and §7.2 say "tick_floor" without naming a tick size. The desk snaps NSE cash
levels to **₹0.05** (`baskfy_core.vbt.config.TICK_INR`, the swing book's value too). The research
simulator floors to **one paisa** (`research/volume-breakout/vbt/sim.py`'s
`_tick(x) = floor(x * 100) / 100`). Every stop and every trailing trigger in `out/final_trades.csv`
therefore sits on a paisa, and TW2's goldens are supposed to be a tick comparison.

**Taken.** Every level function takes the tick as a parameter, defaulting to
`baskfy_core.twt.config.TICK_INR` [`"0.05"`], and `RESEARCH_TICK_INR` [`"0.01"`] exists **for
exactly one caller**, TW2's golden parameter set — the same shape, and the same justification, as
`EntryConfig.research_min_turnover_inr` (TW0.3). `test_twt_no_literals.py` asserts its only
production reference is `config.py` itself.

**Rejected.** (a) Hard-coding ₹0.05 — then TW2 cannot reproduce the study at all and every golden
trade is off by up to four paise on both legs. (b) Shipping ₹0.01 as the sleeve's tick — the
exchange does not quote cash equities in paise, and a GTT armed at a price the exchange will not
accept is a stop that does not exist.

**Reversal.** One constant, and the parameter every function already takes.

## TW1.2 — `max_open_positions` governs the slot ceiling; it is not floored at `max_slots` · ⚠ UNREVIEWED

**Context.** `04` §10.1 reads `open + lined >= max_open_positions -> SLOTS_FULL`, and `02` bounds
that `tw_config` setting at 15 from above. `sizing.max_slots` is 10 and fixes the slot *size*
(a tenth of equity). VBT-1's `build_entries` takes `min(setting, max_slots)` so the setting can only
ever lower the book.

**Taken.** TWT follows `04` §10.1 literally: the setting governs, defaulting to `max_slots`. A book
run at 15 names still sizes each line at a tenth of equity, so the eleventh line is refused by
`EXPOSURE_FULL` or by cash rather than by a silently smaller slot — the refusal a person can read.

**Rejected.** Copying VBT-1's `min`. There the setting is the weaker of a pair by that pack's own
design; here `04` §10.1 names the setting and `02` gives it a ceiling of 15, which would mean
nothing if the code floored it at 10.

**Reversal.** One `min` call in `build_entries`, and the test that reads it.

## TW1.3 — A corporate action's trigger is derived from the re-derived high alone · ⚠ UNREVIEWED

**Context.** `04` §7.2's ratchet takes `raw_trigger = max(stop_in_force, high_since x (1 - trail))`.
`04` §7.3 and TW0.7 then say that when `adj_factor` moves, the re-derived trigger may come out
**below** the resting one — which is what a split does — and that the line is then not emitted and
`TWT_ADJUSTMENT_RESET` is raised instead. **Those two cannot both hold in one function:** the `max`
floors the result at the resting stop, so §7.3's second branch would be unreachable and the alert
would never fire.

**Taken.** `exits.ratchet` keeps §7.2 exactly (the `max`, then the clamp). `exits.on_adjustment`
derives the trigger from the **re-derived high alone** — the same trail and the same clamp, no
`max` — and then refuses: above the resting stop it is a `RAISE_GTT_STOP`, at or below it is an
alert and nothing else. The protection against a falling stop is the refusal, not an arithmetic that
cannot produce the number, which is also what makes the number visible to the person who has to look
at the resting order.

**Rejected.** (a) Reusing `ratchet` unchanged — §7.3's alert branch becomes dead code and a split
silently leaves a stale GTT resting with nothing said. (b) Emitting the lower trigger — the one
thing this sleeve must never do on its own (TW0.7).

**Reversal.** One function; `tw_position` still carries `entry_adj_factor` and `high_since`, so any
past adjustment can be re-read.

## TW1.4 — The one branch of the ratchet that can lower a stop is surfaced, not swallowed · ⚠ UNREVIEWED

**Context.** `04` §7.2's fallback — `tick_floor(close x close_clamp_fallback)` when
`raw_trigger >= close` — can produce a number **below** the stop in force. It can only happen when
the close is at or under the resting stop, which is a state an open position should not be in: the
GTT should already have fired. The research code carries the number regardless.

**Taken.** `exits.ratchet` reproduces the expression exactly (TW2's goldens need it), and returns
three things beside it: `next_trigger` (the expression), `stop_in_force` (`max` with the resting
stop — **never** below it), and `clamped_below_stop`, the flag for the pathological case. `plan.
exit_lines` emits a `RAISE_GTT_STOP` only when `next_trigger` is strictly above the resting trigger.
Three places say a stop never falls, as `04` §7.2 asks, and the third is the desk's own refusal.

Related and recorded here rather than separately: `signals.entry_events` stores
`sessions_out_before` with the research's own `since_out` meaning — **the number of sessions
immediately before this one on which the state was false**, so a state true yesterday reads 0 and
`04` §3.4's five-session gap is `>= 5`. It is null when the name has never been in the state, which
is why TW0.6's listing clause is a separate condition rather than a default for that null.

**Reversal.** None wanted; every field is additive.

## TW1.5 — Two schema values that nothing in TWT-1 produces, and the tests that say so · ⚠ UNREVIEWED

**Context.** `03` §7 check-constrains `tw_plan_line.kind` and `tw_plan_skip.reason` to lists that are
each one value wider than the rules can emit: `SELL_AT_OPEN` (the strategy has no end-of-day sell
rule) and `TURNOVER_CAP` (`04` §10.1 says the turnover cap alone binding is *not* a skip, and §6.2
says a line that cannot clear the floor is `BELOW_MIN_TRADE_VALUE` — whichever cap made it small).

**Taken.** Both are members of the enums, so the schema and the code agree and a person can record
one by hand; **neither is emitted by any rule**, and the tests assert the absence rather than trust
it (`test_twt_plan.py`, and TW10 for the route). The size's own `caps_applied` names the turnover cap
in the line's note when it bound, which is the information §10.1 wanted kept.

**Rejected.** Dropping either from the enum — then a `MANUAL` exit line needs a migration, and the
check constraint of `03` §7 has values no enum covers.

**Reversal.** None wanted.

## TW1.6 — The evening plan cannot judge a bar that has not printed, and does not pretend to · ⚠ UNREVIEWED

**Context.** `04` §10.1 checks "no bar on the entry session -> `NO_BAR`" and "`open == high == low`
-> `LOCKED_UPPER_CIRCUIT`" in its skip order. The **evening** plan is built after the signal
session's close, for tomorrow's open; tomorrow has no bar. The backtest, TW9 and the morning
re-size all know the bar.

**Taken.** `plan.Candidate.entry_bar` is `EntryBar | None`. `None` means "not yet knowable" and
**neither bar-shaped skip fires**; a present `EntryBar` is judged exactly as §10.1 says. An absence
of information is never a refusal, and `04` §11.3 is unaffected either way — the morning reads the
same signal session, re-sizes, and does not re-detect.

**Rejected.** (a) Skipping `NO_BAR` in the evening because the bar is missing — every signal would
be refused every evening. (b) A second `build_entries` for the evening — two implementations of
`04` §10.1, which is the thing §10.5 exists to prevent.

**Reversal.** One optional field.

Also added in TW1 and small enough not to need its own entry: `BreadthConfig.pct_decimals` [4],
because `04` §4.2's "four decimal places" is a number and G7 forbids a literal outside `config.py`.
`breadth.breadth_series` rounds to it **before** the gate reads the value, so the number a page shows
and the number the gate decided on are the same number; `test_twt_breadth.py` asserts the series and
the single-session reading agree on every session.

---

## The pack's own standing position on two things it was not asked

**There is no paper phase and this file does not re-open it.** Maulik decided it on 11 Sep 2026,
before the run started; `02` §3 is written to that decision. What the pack *does* record is the one
consequence worth naming: the ratchet will first run with money behind it, which is why TW7's
fill-day rule and TW10's sweep exist.

**The run never flips a flag and never sets a capital.** Root `CLAUDE.md` safety rails. Nothing in
this file is a reason to.

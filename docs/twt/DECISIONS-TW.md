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

## TW2.9 — A bar becomes a `Decimal` through its shortest repr, which is what makes the study's epsilon reproducible · ⚠ UNREVIEWED

**Context.** House rule 9 says money and prices are never float; the study's panel is a matrix of
`float64`. So `baskfy_core.twt.backtest` has to convert at the boundary, and there are two ways to
do it. `baskfy_core.vbt.backtest._dec` uses `Decimal(x)` — exact about the **double** — and its
docstring calls the exactness a virtue. For TWT it would have been a bug.

The research's tick function is `_tick(x) = floor(x * 100 + 1e-9) / 100`. **The `1e-9` is not
noise-tolerance for nothing:** a double that means ₹100.05 is `100.04999999999999715…`, so
`x * 0.8` comes out a hair under ₹80.04 and a bare floor returns ₹80.03. The epsilon puts it back.
The initial stop is `open × 0.8` and the trail is `high_since × 0.8`, and a 2-decimal price whose
paise end in 0 or 5 multiplies to an exact paisa — roughly **one entry in five** — so the choice is
worth about a paisa on a fifth of the stops, and a stop one paisa out is a different exit session
and a different trade.

**Taken.** `_dec(printed) = Decimal(str(printed))` — the float read as the decimal it prints as —
and then `exits.tick_floor` with no epsilon at all. The two corrections are the same correction:
`str()` recovers ₹100.05, `100.05 × 0.8` is exactly `80.04` in decimal, and the floor returns
`80.04` without needing a guard. Reproducing the study then needs **none of its float machinery**,
only its rules.

**Measured.** Entry and exit prices agree with `out/final_trades.csv` **to the paisa on all 164
trades, both legs**, and the 137/15/2/10 exit-label split is identical. With `Decimal(x)` instead,
the stops move and the trade list is a different book — which is why this is an entry and not a
comment.

**Rejected.** (a) `Decimal(x)`, as VBT-1 does — exact about the wrong thing, here. (b) Carrying the
research's `+1e-9` into `exits.tick_floor` — it would put an epsilon into the **live** level the
desk arms a GTT at, to fix a float problem this package does not have. (c) Rounding every bar to
the tick on the way in — the adjusted series is not on a paisa grid and never was.

**Reversal.** One function, `backtest._dec`. If it is ever changed, TW2's golden trade list is the
test that says so, and it will say so loudly.

## TW2.10 — Three fields still differ from the goldens, every one of them the study's own float error · ⚠ UNREVIEWED

**The reproduction.** 164 of 164 trades matched on `(symbol, entry_date)`; **zero missing, zero
extra**. Of the eight fields `twt_compare.COMPARED_FIELDS` checks, **five carry no difference at
all**: `exit_date`, `exit_price`, `quantity`, `hold_sessions` and `reason`. `r_mult`, which the
comparer does not check, agrees on all 164 to 1e-9.

**What remains, with its size and its cause.**

| field | trades differing | largest difference | tolerance | in ulps of the study's own double |
|---|---|---|---|---|
| `entry_price` | 44 | `5E-13` | 0.01 | 1.10 |
| `pnl_inr` | 153 | `2E-10` | 1 | ~2,000 |
| `return_pct` | 164 | `8.3E-14` | 0.01 | ~2,300 |

**Cause.** This engine's arithmetic is exact decimal; the study's is `float64`, and
`out/final_trades.csv` stores the shortest repr of the study's doubles. `entry_price` is one
multiplication — `open × 1.0025` — so it is off by at most **one ulp**, which is the study rounding
a product this engine does not have to round (`2436.9271249999997` against `2436.927125`). `pnl`
and `ret_pct` are worse in ulps and still microscopic in rupees because they **subtract two numbers
of similar size**: `price × qty × (1 - cost)` minus `entry × qty`, each around ₹1e5, to get a
number sometimes around ₹1e2. That is textbook cancellation, and it amplifies the study's relative
error to about `3e-13` — still a hundredth of a microrupee on the worst trade.

**This is not a delta the sleeve owes anyone.** The exact value is ours; the golden is the float's
approximation of it. There is no arithmetic available to this engine that would reproduce the
study's rounding without reproducing its float chain, and reproducing a float chain is not what
`04` asks for.

**No tolerance was widened to accommodate any of it.** The three allowances (`0.01`, `1`, `0.01`)
were written into `tools/twt/twt_compare.py` by the harness leaf *before* this engine existed, and
`git diff tools/twt/` is empty. Every difference is four to eleven orders of magnitude inside its
allowance.

**Not a blocker for TW9.** `06` TW2 says a difference that cannot be explained blocks TW9. **There
is none**: every one of the 361 is the same float-versus-decimal fact, sized and named above.

## TW2.11 — The trail exit is reported `STOP_HIT`, and TW2.4's limit is accepted rather than worked around · ⚠ UNREVIEWED

**Context.** TW2.4 recorded that `out/final_trades.csv` has **no label for the trail**: the
research raises `pos.stop` as the trail ratchets and reports every exit through it as `stop`. A
`TRAIL_HIT` on this side would show up as **137 differences that are not differences**.

**Taken.** `baskfy_core.twt.exits.ExitReason` has no `TRAIL_HIT` member and the backtest does not
invent one. A stop that fired is `STOP_HIT` (touched intraday) or `STOP_GAP` (the open was already
through it) or `STOP_DAY0` (the fill session's own low), which is the split the study makes and the
split that changes what the **fill price** was. Measured: `STOP_HIT` 137, `STOP_GAP` 15,
`STOP_DAY0` 2, `END_OF_RUN` 10 — the goldens' own four counts, exactly.

**Why this is the right way round.** "Trail or disaster" is a property of the *stop's history*, not
of the exit event: the resting level is one number and the book has been moving it since entry. It
is recoverable at any time from `initial_stop` against `exit_price` — a trail exit is one whose fill
is above the initial stop — so nothing is lost by not labelling it, and a label the goldens cannot
grade is a label nobody checks.

**Reversal.** If the distinction is ever wanted on the page, it is a derived property on
`BacktestTrade`, not a sixth `ExitReason`; adding the enum member is what would break the goldens.

## TW2.12 — The backtest carries the clamp branch that can lower a stop; the plan refuses it; it fired zero times · ⚠ UNREVIEWED

**Context.** TW1.4 recorded that `04` §7.2's fallback — `tick_floor(close × close_clamp_fallback)`
when the raw trigger is at or above the close — can produce a trigger **below** the stop in force,
and that `exits.ratchet` therefore returns three numbers: `next_trigger` (the expression as
written), `stop_in_force` (`max` with the resting stop, never below it) and `clamped_below_stop`.
The backtest has to pick one, and the two picks are different books.

**Taken.** The backtest writes **`next_trigger`** — the research's own behaviour, which is what the
goldens encode — and **counts** every occurrence on `BacktestResult.clamped_below_stop`. The *plan*
(`plan.exit_lines`) is where the refusal lives: it emits a `RAISE_GTT_STOP` only when the trigger is
strictly above the resting one, so a live stop still cannot fall. Three statements of "a stop never
falls" (TW1.4) are untouched; what changes is that the **simulated** book is allowed to reproduce
the study and is made to say so.

**Measured: zero.** Over the study's whole 2017 → 2026 run the branch fired **0 times**, so the two
readings produce the same 164 trades and the choice costs nothing on this panel. It is reachable in
principle — it needs a close that has fallen to within 0.1 % of the resting stop without the session
ever trading through it — and on the plant's bars it may not stay at zero, which is why it is
counted rather than asserted away.

**Rejected.** (a) Writing `stop_in_force` — safer-sounding, and it would silently make the backtest
a different strategy from the one `01` §5's numbers describe. (b) Leaving the case uncounted —
that is the version where the day it stops being zero, nobody finds out.

**Reversal.** One assignment in `run_backtest`, and the counter says how much it would move.

## TW2.13 — What TW9 needs to run this engine over the plant's bars, and the four things TW2 could not measure for it · ⚠ UNREVIEWED

**The engine is parameterised, not forked.** `06` § TW9 asks for the same functions over
`ohlcv_daily`. The whole difference is a `BacktestParams` and one keyword:

1. bars from `ohlcv_daily` with `indicators.REQUIRED_COLUMNS` plus `is_etf` and `adj_factor`, put
   on the run's own calendar by `calendar.drop_thin_sessions`;
2. `signals.with_twt_columns`, then `signals.signal_mask(detected, config)` with **no** `floor_inr`
   — the shipped ₹5 crore, where TW2 passes the research's ₹2 crore;
3. `breadth.breadth_series` → `backtest.gate_vector(breadth, panel.sessions)`;
4. `BacktestParams(start=config.backtest.start, sleeve_inr=…, tick=Decimal(TICK_INR),
   cost_pct_per_side=config.costs.cost_bps_per_side / 100, config=config)` — **all four defaults
   are already the shipped ones**, so a TW9 run that passes nothing is the shipped sleeve;
5. `summarise(result).to_json()` into `tw_backtest_run.stats`, `source = PLANT`.

`PANEL_COLUMNS` is asserted to be a subset of what `with_twt_columns` produces, so there is no
column the study's panel has and the plant lacks. `test_twt_backtest.py::
TestTheEngineRunsOnThePlantsShape` runs the whole chain from bare OHLCV.

**The four things TW2 cannot tell TW9, and must not be read as having told it.**

* **The tick.** TW2 ran at ₹0.01 (TW1.1); the plant runs at ₹0.05. Every stop moves by up to four
  paise, which moves which sessions stop out. The drift TW9 measures against `01` §6 will contain
  this, and it is **not** a plant defect.
* **The floor.** ₹2 crore against ₹5 crore (TW0.3) — a deliberately different, smaller book.
* **`adj_factor` is 1 everywhere in the research panel** (TW2.1). On the plant's bars it is real,
  `close` and `close_raw` diverge, and `04` §7.3's corporate-action branch becomes reachable
  *inside* a hold for the first time. Nothing in TW2 exercises it.
* **Two numbers measured at zero here are zero *on this panel* only**: the ETF breadth denominator
  (TW2.2 — the export barely carries ETFs, the plant does) and `clamped_below_stop` (TW2.12).

**Two shape differences from `baskfy_core.vbt.backtest`, deliberate.** There is no `_Working`
order book and no `orders_offered` — TWT-1's entry is `NEXT_OPEN` and nothing else (`04` §5.1), so
every candidate that clears the caps and prints a bar fills, and a "fill rate" would be 100 % by
construction rather than a measurement. And there is no `queued_exit`: TWT-1 has no end-of-day exit
rule at all, which is the same absence `exits.Action`'s three members record.

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

## TW4.1 — The detector's tests live in `services/api/tests`, the step's in `services/worker/tests` · ⚠ UNREVIEWED

**Context.** `gates/twt-4.md` names its check commands, and five of the ten run
`uv run pytest packages/core/tests services/api/tests`. The module under test is a **worker**
task, and its sibling VB4's tests live in `services/worker/tests/test_vbt_detect.py`. Put TW4's
tests where the sibling's are and five gate commands collect nothing, which pytest reports as
"no tests ran" — a gate that cannot fail is not a gate.

**Taken.** The detector's behaviour — idempotence, the funnel, the ratchet, the split, the two
look-ahead measurements, the retry — is `services/api/tests/test_twt_detect.py`, with the
self-contained database bootstrap `packages/core/tests/test_twt_schema.py` established (migrate
to head, never drop, every test inside a transaction that is rolled back). The **wrapper's** one
guarantee — a detector that raises leaves the run SUCCEEDED — is
`services/worker/tests/test_twt_step.py`, where `PipelineRun` and the step fixtures already are
and where `gates/twt-4.md` G6 looks for it.

`services/api/tests` already imports `baskfy_worker` in fourteen modules, so this is the tree's
own convention for a cross-service database test rather than a new one.

**Rejected.** (a) Putting them in `services/worker/tests` and editing the gate's commands — the
gate file is the definition of done and an agent that rewrites its own acceptance check has
stopped being checked. (b) Duplicating the suite in both trees — two copies of a fixture is two
ways for the fixture to be wrong. (c) `packages/core/tests` — that tree is the pure core's, and
a worker task's database test does not belong in the package whose law is that it touches
nothing.

**Reversal.** `git mv` the module into `services/worker/tests/`, where the shared `session`
fixture would replace the bootstrap, and widen the five gate commands to include that directory.

## TW4.2 — A signal for a name that has never held the state still carries a `sessions_out_before` · ⚠ UNREVIEWED

**Context.** `03` §3 makes `sessions_out_before` NOT NULL and ">= 5 by construction". TW1.4
records that `signals.entry_events` leaves it **null** when the name has never been in the state
at all, because the research's counter is "sessions since the state was last true" and there is
no last. On a 260-session panel that is the ordinary case: this book's names are tight for a few
days a year.

**Taken.** The task writes `sessions_listed - 1` — every session the name has been listed **and
out** — when the core's counter is null. It is the same quantity the counter measures, taken
from the only origin that exists for such a name, and the entry event has already required it to
be at least `entry_min_sessions_out`, so the column's stated invariant still holds. The state row
beside it carries `sessions_in_state = 1`, so the two together still say exactly what happened.
`test_twt_detect.py::test_sessions_out_before_counts_the_sessions_the_state_was_false` asserts
both branches side by side, on two names of one panel.

**Rejected.** (a) Making the column nullable — a migration, and it would make "how cold was this
name" unanswerable for precisely the coldest names. (b) Writing `entry_min_sessions_out` — a
number the system did not measure, stored as though it had. (c) Writing a sentinel like `-1` —
the CHECK forbids it, and rightly.

**Reversal.** One `if` in `tasks/twt.py::signal_rows`, plus the column's nullability if the other
branch is preferred.

## TW4.3 — The 21:00 retry asks the breadth table, not the signal table · ⚠ UNREVIEWED

**Context.** VB4's Beat retry asks `published_signal_count` before it works, because re-detecting
a session the chain already wrote costs minutes of Polars to arrive at the same rows. TWT-1
signals about **eighteen times a year** (164 trades over nine years, `01` §6), so "this session
has no signal rows" is what a session that ran perfectly looks like on almost every weeknight.
Keyed on signals, the retry would densify 260 sessions over the whole cash universe every night.

**Taken.** `published_session_count` counts `tw_breadth_daily` rows, which is exactly one per
session the job has seen — including the thin ones it wrote a shut-gate row for and refused to
trade. The Beat task and `make twt` share the one helper (`tasks.twt.detect_session`, which lives in
the job's own module and not the CLI's), so the retry and
the CLI cannot come to disagree about when a session counts as done, and
`test_twt_detect.py::TestTheRetryAsksBeforeItWorks` asserts it on a panel whose only name is
never tight — no states, no signals, and the retry still says "already detected".

**Rejected.** (a) `tw_state_daily` — about fifty rows a day, but **zero** on a thin session, so
a muhurat session would be re-detected every night forever. (b) `tw_session` — TW5 and TW6 own
that row and it does not exist after detection alone. (c) Keying on the pipeline step — the
retry exists precisely for the night the chain did not get that far.

**Reversal.** One `select` in `tasks/twt.py::published_session_count`.

## TW4.4 — The book does not ratchet on a thin session · ⚠ UNREVIEWED

**Context.** `04` §2.1 removes muhurat and special-Saturday sessions from the rolling calendar:
about 200 names print against about 1,900, and such a session "is not a trading session for this
strategy". `04` §7.2's ratchet says "after every close on which the position is open" and does
not say which closes count.

**Taken.** A thin session writes its `tw_breadth_daily` row for the record — `thin_session = true`,
`gate = SHUT`, null percentages — and **returns before the ratchet**. A trail that moved on a
200-name session would be a stop derived from a day the strategy's own calendar says did not
happen, and the stop is the thing this sleeve cannot get wrong. Nothing is lost: the next real
session's ratchet reads `high_since` and the resting stop and produces the same level it would
have, because the arithmetic is a maximum over the hold rather than an accumulation of deltas.

**Rejected.** (a) Ratcheting anyway — a muhurat session's high is a real exchange print, so the
answer would not be *wrong*; it would be a level nobody could reconcile with the calendar the
rest of the sleeve reads, and `04` §2.1 exists because such a session poisons every window that
spans it. (b) Writing no row at all — a hole in the history that reads like a failed job, which
is the thing `03` §4's `thin_session` column exists to prevent.

**Reversal.** Move the `run_twt_ratchet` call above the thin-session branch in
`tasks/twt.py::run_detect_twt`. Two lines.

## TW4.5 — `COMPUTE_TWT` joins `POST_PUBLISH_STEPS`, and one sibling assertion had to stop pinning a position · ⚠ UNREVIEWED

**Context.** `steps.py` asks for this by name where `POST_PUBLISH_STEPS` is defined: *"A step
added after these must either join this set or be a step the run's success depends on, which is a
decision, not an edit."* VB4 made that decision and recorded it as DECISIONS-VB VB0.5.

**Taken.** `COMPUTE_TWT` is fourteenth, after `COMPUTE_VBT`, and is in `POST_PUBLISH_STEPS`;
`run_compute_twt_step` records its own failure and returns. Detection writes no money and nothing
downstream depends on it, while a nightly that failed is a screener serving yesterday to
everybody.

**One sibling test had to change, and it is the change `steps.py` predicted.**
`test_vbt_detect.py` asserted `chain[-1] is PipelineStep.COMPUTE_VBT`. That pins a *position*,
and the same file in `steps.py` says the property "was never the position". It now asserts
`set(chain[chain.index(COMPUTE_VBT):]) <= POST_PUBLISH_STEPS` — strictly stronger, because it
holds for every step added after it too — with a comment saying what it used to say and why.
`test_pipeline_chain.py`'s three chain assertions gained `compute_twt` and its count went from
thirteen to fourteen, which is the same edit VB4 made to them. No sibling *code* is touched.

**Rejected.** Making the step able to fail the run. A sleeve that has never traded must not be
able to hold back `data_version`; the trade is not close.

**Reversal.** Remove the member from `PipelineStep`, the set and the orchestrator, and restore
the two test files' previous wording. Nothing stored changes shape.

## TW4.6 — A handled corporate action does not rewrite `entry_adj_factor` · ⚠ UNREVIEWED

**Context.** `04` §7.3 detects an action by comparing the as-of row's `adj_factor` with the
position's `entry_adj_factor` (TW3.2). Nothing says what happens to that column afterwards. Leave
it and the branch runs every evening for the rest of the hold; rewrite it and the branch runs once.

**Taken.** It is **not** rewritten. The column means what its name says — the factor the line was
bought at — and `03` §5's argument for storing `high_since` is the same argument: when a
corporate action rewrites the adjusted series underneath a 600-session hold, "what was this stop
derived from" must still have an answer, and a column that has been overwritten does not have one.

Running the branch every evening is not a cost: `exits.on_adjustment` is a pure function of the
re-derived high and the resting stop, so it returns the same answer every night until a person
changes one of them — which is exactly the state `04` §7.3 wants, a position frozen at its
pre-split stop with an alert against it until somebody looks. Once the stop has been re-armed in
post-split money the same branch starts emitting raises again, so the ratchet resumes without
anything being rewritten. `test_twt_detect.py` asserts both halves: the refusal, and the raise
after a re-arm.

**Rejected.** (a) Setting `entry_adj_factor` to the as-of factor once handled — it makes the
field a lie, and it makes the alert fire exactly once, which is the wrong number of times for a
condition a person has not yet acted on. (b) A separate `adjustment_handled_on` column — a
migration for a state that is already derivable.

**Reversal.** One assignment in `run_twt_ratchet`, plus a column rename if the field is to keep
meaning what it says.

## TW4.7 — `tw_state_daily` stores the adjusted comparands; only `tw_signal_daily`'s two levels are converted · ⚠ UNREVIEWED

**Context.** `03` §10 is categorical: *"Levels leave core adjusted and are converted by the task
(`level / adj_factor` of the as-of row) before they are stored or shown."* `03` §2 is equally
categorical that `tw_state_daily` holds `week_range_pct` = `(max/min - 1) x 100` and
`month_low_ratio` = `close / month_low_3`, stored "because the one rule anybody will dispute is
this one". Both cannot be satisfied for the same columns: converting `month_low_3` while `close`
is stored adjusted makes the stored ratio disagree with the stored columns it is computed from.

**Taken.** `tw_state_daily` is the **record of what the rule compared** and is kept entirely in
the adjusted space — the bar, `week_close_0/1/2`, `month_low_3`, `sma_dma` — with `adj_factor`
beside it so any of them can be turned into an exchange price by a reader who wants one.
`tw_signal_daily`'s `entry_reference_close` and `stop_preview` are converted, because `03` §3
already says the first is "as an exchange price" and the second is a preview of a level a broker
would be sent. So §10's rule applies where a level *leaves* the sleeve and not where a comparison
is being recorded.
`test_twt_detect.py::test_the_stored_state_row_is_auditable_from_its_own_columns` re-derives
`month_low_ratio` from the row and asserts equality, which is the property this choice protects.

**Rejected.** (a) Converting everything — the two stored ratios then disagree with the columns
beside them on every split, which is the one dispute the table exists to settle. (b) Storing both
spaces — six more columns for a number `adj_factor` already makes recoverable.

**Reversal.** Convert in `state_rows` and recompute the two ratios from the converted values;
`03` §2's column notes would need a line each saying which space they are in.

## TW4.8 — The stop the ratchet trails against is the higher of `stop_price` and `gtt_trigger` · ⚠ UNREVIEWED

**Context.** `04` §7.2's `raw_trigger = max(stop_in_force, ...)` and `03` §5's "`next_trigger` …
null when it does not exceed `gtt_trigger`" name two different columns. They agree on an ordinary
evening. They differ for one session whenever a raise is in flight — the desk has replaced the
resting order but the book row has not caught up, or the reverse.

**Taken.** `run_twt_ratchet` reads `max(stop_price, gtt_trigger)` as the stop in force. A stop
never falls (`04` §7.2), so where the two disagree the higher is the one the position is
*actually* protected at by at least one of the two systems, and trailing against the lower would
propose a raise that the desk would then refuse — `RAISE_GTT_STOP` at or below the resting
trigger is `BLOCKED` (`06` § TW6). A proposal the desk is guaranteed to block is a line on the
morning page that wastes a person's attention.

**Rejected.** (a) `stop_price` alone — `03` §5's own sentence names `gtt_trigger`. (b)
`gtt_trigger` alone — it is null on a position whose GTT has not been armed yet, which is the
`ARM_GTT` case of `04` §10.2 and a real state after a fill.

**Reversal.** One `max` in `run_twt_ratchet`.

## TW4.9 — `locked_upper_circuit` is `03` §2's circuit band, not `04` §5.2's locked open · ⚠ UNREVIEWED

**Context.** Two documents define the column's condition differently. `03` §2 spells it out:
`upper_circuit > 0 and high >= upper_circuit`. `04` §5.2 defines the *skip* `LOCKED_UPPER_CIRCUIT`
as a name whose session opens `open == high == low`, and TW1's core computes exactly that as
`limit_locked`.

**Taken.** The stored column follows `03` §2, the document that names the column. The two are
different questions and both are worth having: the stored flag says the name traded into its band
on the signal session, which is context for a person reading the page; the plan's own skip is
computed from the **entry** session's bar (`04` §10.1, TW1.6), which the evening does not have
yet and which `04` §5.2 is about. The task multiplies `upper_circuit` by the row's `adj_factor`
on the way in, because the band is an exchange print with no adjusted twin while `high` has been
adjusted in place — unconverted, the morning after a 1:2 split would report the whole market
locked.

**Rejected.** (a) Storing `limit_locked` — then the column's own document is wrong and the
schema's comment describes something else. (b) Storing their disjunction — a flag that means
either of two things means neither.

**Reversal.** Swap the expression in `state_rows` for `pl.col("limit_locked")`, and correct `03`
§2's description to match.

---

## TW5.1 — The loader answers ₹0 for an unseeded sleeve where the read path raises · ⚠ UNREVIEWED

**Context.** TW3 decided that `baskfy_api.twt_settings.read_config` raises `TwtConfigNotSeeded`
rather than creating a row: a request handler that seeds is a handler that writes on a GET, and
the row it writes carries defaults nobody chose. TW5's loader reads the same row for a different
purpose — to answer "how much money does this sleeve have" — and a sleeve nobody has seeded and a
sleeve seeded at ₹0 are the same amount of money.

**Taken.** `twt_sleeve.sleeve_capital` catches `TwtConfigNotSeeded` and answers `Decimal(0)`;
`slot_multiplier` answers 1 for the same row. Both states therefore produce the **same plan**: one
with every signal skipped `NO_SLEEVE_CAPITAL` (`04` §9.3), and
`test_twt_sleeve_db.py::test_an_unseeded_sleeve_also_skips_no_sleeve_capital` asserts that the two
are indistinguishable. The read path keeps raising, because a person asking the settings page a
question deserves "run `make seed`" rather than a form full of zeros. VBT-1's `vbt_sleeve` already
answers zero for the same reason and this keeps the two loaders the same shape.

**Why not the other way.** An evening job that raised on an unseeded row would lose the session's
stored states, signals and breadth — rows that are snapshots and are never recomputed for a past
date (`03` §2) — in order to protect a number that is zero either way. The failure would be loud
and the cost would be a hole in the record.

**Rejected.** (a) Letting the loader raise and catching it in TW4's task — then every caller
repeats the branch, and the one that forgets is the one that runs at 19:00. (b) Seeding the row
from the loader — a loader that writes, and the row would carry defaults nobody chose.

**Reversal.** Delete the `try`/`except` in `sleeve_capital` and `slot_multiplier`; the two
functions are the only ones that swallow it, and the swallow is named rather than bare (house
rule 3 forbids the silent one, not the argued one).

---

## TW5.2 — The sleeve is a `MY_STRATEGY` capital portfolio by declaration, not by a `portfolio` row · ⚠ UNREVIEWED

**Context.** `04` §9.3 and `06` TW5 both say the sleeve "is a `MY_STRATEGY` capital portfolio in
the M34 / `PORTFOLIO_REDESIGN` sense, exactly as the swing and VBT sleeves are". The two siblings
do **not** do the same thing about it: `broker_holdings_sync.file_swing_positions` files every open
swing position into a real `portfolio` row named "Swing", and VBT-1 files nothing at all — its
`vbt_sleeve` simply never reads the account.

**Taken.** VBT-1's reading. `twt_sleeve` declares `TWT_PORTFOLIO_KIND = PortfolioKind.CAPITAL`,
`TWT_PORTFOLIO_SOURCE = PortfolioSource.MY_STRATEGY` and `TWT_PORTFOLIO_NAME`, the test class
`TestTheSleeveIsAMyStrategyCapitalPortfolio` pins both to the enums and then proves the substance —
four names the account holds through another book move this sleeve's equity by nothing, take no
slot, and produce no line. What the phrase *means* for arithmetic is "its own capital, its own
realised profit, its own marked positions, and never the account's holdings", and that is what is
asserted.

**Rejected.** (a) Filing `tw_position` rows into a `portfolio` row now — it is a write into the
portfolio forest by a module whose gate is about money and safety, it needs the broker-account
attribution `file_swing_positions` carries, and it belongs beside the page that would display it.
(b) Declaring nothing and relying on the docstring — then `06` TW5's bullet is a sentence with no
test under it, which is how a shape gets copied without its rule.

**Reversal.** Add a `twt_portfolio` / `file_twt_positions` pair shaped exactly like the swing one,
using the three constants this module already names. **TW8 is the natural place**, since it is the
module that renders the sleeve to a person.

---

## TW5.3 — The countdown is spent by a position id, and `half_size` is both the record and the key · ⚠ UNREVIEWED

**Context.** `04` §6.4 and `03` §1: `first_live_entries_left` moves "once per **filled** entry by
the session that filled it", never by a request and never by a plan nobody confirmed. Three things
have to be impossible — a proposed line counting, a `DRY_RUN` plan counting, and one fill counting
twice — and a rule that is only remembered is a rule that will be broken by the retry.

**Taken.** `count_first_live_entry(session, *, user_id, position_id, session_date, now,
execution_enabled, changed_by)`. Each of the three is structural rather than checked:

* the parameter is a **`position_id`**, and a proposed line has no `tw_position` row to have one —
  a caller who tries is answered with a `LookupError`, and the id is scoped to `user_id`, so one
  tenant's fill cannot spend another's countdown (P4.1);
* a `DRY_RUN` or flag-off fill is `simulated`, and a simulated fill counts nothing — which is the
  same sentence as "a `DRY_RUN` plan is full size";
* **`tw_position.half_size` is the idempotency key as well as the record.** A row already marked is
  already counted, so a partial fill that completes later, a retried confirm and a re-run of the
  morning all land on the same answer (house rule 7).

`tw_session.first_live_entries_counted` is incremented by an upsert that adds in the database
rather than a read-modify-write, so the count is right whether the evening job wrote the session
row first or the fill did, and two fills a minute apart cannot both read the same number.

**The audit says `twt-fill`, not `twt-evening`.** `twt_settings.SYSTEM_OWNED_FIELDS` names the
evening job as the field's owner, which is a statement about *who may write it*; the entry is
counted by the fill, and `03` §1b exists so "who counted that entry" has an answer that is true.

**Rejected.** (a) Taking the `TwPosition` object — a transient one would have no id and the
guard becomes a runtime question about ORM state. (b) A separate `counted` boolean column — a
second fact that can disagree with `half_size`, and a migration for something an existing column
already says. (c) Decrementing at confirm rather than at fill — a confirm that the gateway rejects
would spend an entry the book never took.

**Reversal.** One function. Nothing stored changes shape, and `half_size` keeps meaning what `03`
§5 says it means either way.

---

## TW5.4 — `04` §6.3's session cap is counted by `signal_date` · ⚠ UNREVIEWED

**Context.** The cap is "at most three new entries a session, counted as lines in this plan **plus**
the session's already-confirmed or sent orders, whatever plan they came from". `tw_order` carries a
`signal_date` and no session column: the order is proposed by the evening of session *t* and fills
at the open of *t+1*, so "the session" could be read as either.

**Taken.** `entries_already_this_session` counts `tw_order` rows with `state ∈ {CONFIRMED, SENT,
PARTIAL, FILLED}` whose `signal_date` is the plan's own signal session, and `book_state` defaults
`signal_date` to the as-of session so an evening plan needs no second argument. The reading follows
the cap's purpose: it limits how many *new lines one night's signals* may become, and
`uq_tw_order_one_per_signal` is already keyed the same way, so the cap and the idempotency key
count the same population.

A `CONFIRMED` order counts although it has not printed. It has spoken for money and for a slot, and
`04` §6.3's own sentence — "so a fourth confirm of an evening is a refusal, not a surprise" — is
about confirms rather than fills.

**Rejected.** (a) Counting by fill date — then a plan rebuilt in the morning (`04` §11.3) counts
zero until the broker answers, and three confirms plus a fourth all fit. (b) Counting plan lines —
`04` §6.3 says "whatever plan they came from" by name.

**Reversal.** One `where` clause. `tw_order` stores both dates once TW6 writes fills, so any past
session can be re-counted either way.

---

## TW7.1 — `tools/twt` is the sleeve's tool directory, not the goldens harness, and the scan says which · ⚠ UNREVIEWED

**Context.** `docs/twt/06` § TW7 and `FIRST-LIVE-MORNING` §8 both name the sweep
`tools/twt/sweep.py`, and TW9's `make twt-backtest` runner landed in the same directory the same
evening. `test_twt_goldens.py::TestTheHarnessIsWhereItSaysItIs` asserted that
`tools/twt/*.py` is **exactly** TW2's five harness modules and that exactly one function in the
directory writes bytes. Both assertions went red the moment a second kind of tool moved in — not
because anything about the harness changed, but because the test used "the directory" as a proxy
for "the harness".

**Taken.** The proxy is named instead of widened. `HARNESS_MODULES` still holds TW2's five;
`OPERATOR_TOOLS` holds `backtest.py` (TW9) and `sweep.py` (TW7), each with a one-line reason; the
on-disk assertion is still an **exact** set over the union, so an eighth unexplained file still
fails. The writer scan keeps scanning the whole directory and gains an explicit
`_ALLOWED_WRITERS` allowlist of `(file, function)` pairs — `twt_scan.write` to the fixtures,
`backtest.main` to wherever `--json` asked, `sweep.record_alerted` to `data/twt/sweep/` — plus a
new assertion that the *harness's* own writers are still exactly `_THE_ONE_WRITER`, so
lengthening the allowlist for a sibling tool cannot quietly loosen the claim TW2's gate was
written about. `research/` remains untouchable and no test lost teeth.

**Rejected.** (a) Narrowing the two scans to `HARNESS_MODULES` — cheaper, and it would stop
asserting anything at all about the two files most likely to write somewhere careless. (b) Moving
the sweep out of `tools/twt/` — the runbook a person reads at 15:15 gives that path literally, and
a runbook made wrong to keep a test green is the wrong trade. (c) Leaving the suite red and
attributing it — half of it is TW9's and half is TW7's, and neither module's gate can be ticked
while it is.

**Attribution.** `tools/twt/backtest.py` is **not** TW7's file. TW7 changed the test so that both
new tools are accounted for; if TW9's session edits the same constants, the two edits are to the
same three names and the later one wins.

**Reversal.** Delete `OPERATOR_TOOLS` and `_ALLOWED_WRITERS` and restore the two literals. Nothing
in the sleeve's code depends on either.

---

## TW7.2 — The sweep's day key is a file, not a `tw_session` column · ⚠ UNREVIEWED

**Context.** `docs/twt/06` § TW7 asks for a sweep that is "idempotent and keyed on the day" —
running it twice re-arms nothing twice and raises nothing twice. Re-arming is idempotent by
construction: the sweep re-reads the book, and a line that was armed is no longer naked. Alerting
is not: a second run over a book the desk could not fix would page a second time about the same
position. Something has to remember what this day was already told.

**Taken.** An injected `SweepJournal` protocol with two implementations — `MemoryJournal` for a
process that owns its run, and `FileJournal` for the CLI, one JSON file per IST date under
`data/twt/sweep/` (untracked). It is keyed on `(day, alert, position)` and never on the day alone:
a **second** line going naked at 15:25 is a new fault, and `FIRST-LIVE-MORNING` §9.1 case 2 says
that is the likely one on this sleeve, on up to ten lines a session for months.

**Rejected.** (a) A `tw_session.swept_at` column — the durable, "proper" answer, and it needs a
migration on a table TW6 is writing in a parallel session tonight. Two agents adding revisions to
one tree is how a repository gets two heads. (b) Reusing `tw_session.naked_at_1515` as the key —
`0` cannot distinguish "the sweep ran and the book was clean" from "the sweep did not run", which
is exactly the distinction a person at 15:16 needs. (c) Suppressing by the day alone — see above;
it swallows the second fault.

**What this costs.** The journal is idempotent across *runs on one machine*, not across machines.
The sweep runs on one box (`docs/08`'s Phase-A), so today that is the same thing. If the desk's
own 15:15 chore and the laptop's command ever both run, the column in (a) is the fix and the
protocol makes it a one-class change.

**Reversal.** Delete `FileJournal`, pass the new implementation. The sweep's own logic does not
move.

---

## TW7.3 — The re-arm is injected, and `build_rearm` is a literal return rather than a soft import · ⚠ UNREVIEWED

**Context.** TW6 owns the desk's GTT arm/cancel paths and was writing them in a parallel session
while TW7 was written. The sweep needs to *ask* for a re-arm without being the thing that arms —
law 2, and non-negotiable 1.

**Taken.** `Rearm = Callable[[PositionId], Awaitable[RearmOutcome]]`, injected into `sweep()`;
the tests supply a fake desk that arms into its own book. `build_rearm()` — the CLI's wiring —
returns `unavailable_rearm` today, a coroutine that **refuses every line with a reason naming
`FIRST-LIVE-MORNING` §9.2 step 3**. When TW6's path lands, that function's body becomes an import
and a return, and nothing else in the module moves.

The placeholder refuses rather than pretends. A placeholder answering `armed=True` would make the
sweep report `naked: 0` over a book it had done nothing to protect, which is worse than no sweep:
the number a person reads at 15:16 would be a lie in the one direction that costs money. For the
same reason a re-arm that answers `armed=True` with a null `gtt_id` is **not believed** and the
line stays in `still_naked`.

**Rejected.** (a) `try: from baskfy_api.twt_gtt import … except ImportError:` — it makes "the
desk's GTT path is wired" a fact nobody can see in a diff, and a typo in the module name would
degrade silently to the placeholder on a live afternoon. (b) Waiting for TW6 and importing
directly — the module would not exist, and TW7's gates could not be run at all. (c) A `Protocol`
for a desk object — one callable is the whole seam; an interface would be shape for its own sake.

**Reversal.** One function body.

---

## TW7.4 — `TWT_POSITION_NAKED` is not keyed on 15:15, and `TWT_GTT_MISSING_AT_1515` is not keyed on the sweep having run · ⚠ UNREVIEWED

**Context.** `FIRST-LIVE-MORNING` §8 says, in bold, that `TWT_POSITION_NAKED` "fires on any naked
line **at any time of day**, not only at 15:15", and that `TWT_GTT_MISSING_AT_1515` is for what
the sweep could not fix. The swing book's equivalent pair is split differently: `SWING_POSITION_
NAKED` is Prometheus-only (a condition over time) and only the 15:20 check is raised in-process.

**Taken.** Both are raised in-process, by the same code path, and the split is by *what the reader
must do* rather than by when they fire. `naked_alert()` takes an `at` and is a pure function of
the book, so any caller at any hour can raise it — the sweep raises it before it tries anything.
`missing_at_1515_alert()` is only ever produced after a re-arm was attempted and carries the
desk's own refusal text plus whether the 15:30 close has already passed, because §9.2's answer
before the close ("re-arm, then arm by hand in Kite") and after it ("consider closing the line")
are different actions.

Not following the swing book's Prometheus-only split, deliberately: this sleeve's stop is 20 %
wide and its book is ten lines, so a naked line here is rarer and worse, and `docs/twt/STATUS.md`
already records that no Prometheus rule file carries a `TWT_*` rule. An alert that waits for a
deployment that has not happened is not an alert.

**Rejected.** (a) One alert with a severity that changes at 15:30 — the runbook has two sections
and a person greps for the name. (b) Raising `TWT_POSITION_NAKED` only when the re-arm fails — it
would then say the same thing as the other one and §8's sentence would be false.

**Note for TW6.** The two `AlertName` members were added by TW6's session, not this one; TW7 reads
them out of `baskfy_worker.alerts` and reads its runbook path out of `ops.RUNBOOKS`, so the two
cannot disagree about either.

**Reversal.** Delete one call.

---

## TW9.1 — `published.py` and `drift.py` join the core, and TW1's exact module list grows by two · ⚠ UNREVIEWED

**Context.** TW9 needs `01` §6's measurements in code (the drift subtracts from them, the terminal
tool prints them beside a fresh run) and the comparison arithmetic in one place. `06` § TW9 names
neither module, and `test_twt_purity.py::EXPECTED_MODULES` asserts the `baskfy_core.twt` package is
**exactly** eleven files, on purpose: "a twelfth module that nobody decided on fails here".

**Taken.** Both, in the core, exactly as `baskfy_core.vbt` carries them — `published.py` (a record
of results, never settings) and `drift.py` (the three deltas, the flag, and both figures the banner
names). `EXPECTED_MODULES` grows to thirteen with the reason written into its comment, and
`__init__` re-exports them. A new test, `packages/core/tests/test_twt_published.py`, asserts
**every transcribed field against `fixtures/twt/golden_metrics.json`** — TW2's committed copy of the
study's own `final_metrics.json` — and the four figures that are not in that file against `01` §7's
own table rows. That is the discipline `baskfy_core.vbt.published` established and the only thing
that makes a hand-copied table safe: the test reads the study, not the module.

**Rejected.** (a) Keeping both in `baskfy_worker.tasks.twt_backtest` — fewer files, and it puts a
pure comparison behind an `async` job so the only way to test the threshold is with a database.
(b) Reading the numbers out of the fixture at runtime — the fixture is a *test* asset and the core
may not read a file (law 1).

**Reversal.** Two modules, four names in `__init__`, two lines in `EXPECTED_MODULES`.

---

## TW9.2 — The job is a worker task and `tools/twt/backtest.py` is its front door · ⚠ UNREVIEWED

**Context.** `06` § TW9 says "`tools/twt/backtest.py` and `make twt-backtest`", and the neighbour
it was modelled on splits the two: `baskfy_worker.tasks.vbt_backtest` holds VB9's job and
`tools/vbt/backtest.py` is a *different* run (the research export, no database). Doing it VBT's way
literally would give TWT two different backtests under one heading.

**Taken.** One run, two names for it. `baskfy_worker.tasks.twt_backtest.run_twt_backtest` is the
job — session in, one appended row out, importable by a test and by a future Celery task —
and `tools/twt/backtest.py` is the terminal front door `make twt-backtest` invokes. TW2's
reproduction over the research panel already exists as `tools/twt/twt_goldens.py` and needs no
second entry point, which is why TWT does not repeat VBT's split.

**Note on the directory.** `tools/twt/` was TW2's harness alone until this evening. TW7's session
hit the same collision and **TW7.1** is the entry that resolves it: `HARNESS_MODULES` keeps TW2's
five, `OPERATOR_TOOLS` names `backtest.py` and `sweep.py`, and `backtest.main` is on the writer
allowlist for `--json`. Nothing here widened a pattern to silence a hit.

**Rejected.** (a) `baskfy_worker.twt_cli --backtest`, which is exactly what `make vbt-backtest`
runs — it is the closer mirror, and `twt_cli.py` was being edited by a parallel session for TW6a's
`make twt-plan`. (b) Putting the whole job in `tools/twt/backtest.py` — a script outside the
package that no test can import is a job nobody can assert anything about.

**Reversal.** Delete the tool and point the Makefile at a `--backtest` flag on `twt_cli`.

---

## TW9.3 — The plant's bars read **22.17 %**, the study published **20.92 %**, and the floor is most of the gap · ⚠ UNREVIEWED

**This is the module's finding. Read it before deciding the plant is wrong.**

The first run over `ohlcv_daily` — 10,127 admitted instruments, 2,393 sessions, 2017-10-16 →
2026-09-04, ₹10 lakh, the shipped ₹5 crore floor and the exchange's ₹0.05 tick:

| | this run | `01` §6 (₹2 cr) | `01` §7's **₹5 cr** row |
|---|---|---|---|
| CAGR | **22.17 %** | 20.92 % | **22.5 %** |
| max drawdown | **-26.47 %** | -24.7 % | **-27 %** |
| trades | **169** | 164 | **169** |
| win rate · profit factor | 42.60 % · 2.78 | 40.9 % · 2.71 | |
| avg hold · time invested | 101.67 sessions · 78.22 % | 104.6 · 78.0 | |
| in sample / out of sample | 13.56 % / 35.15 % | 11.1 % / 36.1 % | |
| gate off | 14.91 % at -48.11 % (249 trades) | 17.2 % at -43 % | |

`drift` reads `cagr_pct_delta = +1.25`, `max_dd_pct_delta = -1.77`, `trades_delta = +5`, and
**`flagged = true`**, because the threshold is one point and `06` § TW9 says to compare against
`01` §6. That is the right row to store and the wrong row to be surprised by.

**The gap is the liquidity floor, and the study measured it itself.** `01` §6's headline is the
**₹2 crore** book; this sleeve ships at **₹5 crore** (`04` §3.5, **TW0.3**). `01` §7's own
sensitivity table gives that variant as **22.5 % at -27 % on 169 trades**. The plant's run lands on
**169 trades exactly**, 0.33 of a point under its CAGR and half a point inside its drawdown. The
run is not reproducing the number it is compared against; it is reproducing the number it was
*asked for*, and the two differ by about the size of the flag.

**What the remaining third of a point is, honestly.** Four things, none of them measurable apart
without a second run each, and **TW2.13** named three in advance: the ₹0.05 tick against the
study's paisa (every stop moves up to four paise, which changes which sessions stop out); real
`adj_factor` where the research panel carried 1 everywhere, so `04` §7.3's corporate-action branch
is reachable inside a hold for the first time; and the plant's own history being a different
ingestion of the same market. The fourth is this run's window: the local snapshot's last bar is
**2026-09-04**, three sessions short of the study's 2026-09-09, so the last three sessions of a
9-year compound are missing from one side of the comparison.

**Two cross-checks that came out clean.** The thin-session rule dropped exactly six dates
(2017-10-19, 2018-11-07, 2024-01-20, 2024-03-02, 2024-05-18, 2025-02-01) — the same six TW2.1
found already absent from the study's panel, two implementations agreeing on `04` §2.1 over the
plant this time. And `clamped_below_stop` is **0** here as it was there (**TW2.12**), so the one
branch that can lower a stop still has not fired on any history this repository holds.

**What was NOT done.** The drift is not re-pointed at the ₹5 crore row. `06` § TW9 says `01` §6,
the page shows `01` §6, and moving the comparison to the row that makes the flag go away is exactly
the "explained away" this module's Goal forbids. `PublishedStudy` now carries
`shipped_floor_cagr_pct`/`_max_drawdown_pct`/`_trades` so the terminal tool *names* the better
comparison under a flagged drift, and this entry is where the explanation lives. If Maulik wants
the page to say it too, that is a TW8 card change and a one-line addition to `Drift`.

**Reversal.** The threshold is a keyword on `compare`; the comparand is `PUBLISHED`.

---

## TW9.4 — TW2.2's ETF warning: measured on the plant, and still zero — for a different reason than it looks · ⚠ UNREVIEWED

**Context.** **TW2.2** measured the ETF contribution to the breadth denominator at zero on the
study's panel and said, in the same entry, that the zero is a property of a sparse export and
**not** of the plant, where ETFs print daily — so TW9 must not inherit it. `gates/twt-9.md` G7 is
that sentence as a gate.

**Taken.** The run measures it. `etf_denominator_delta` adds the refused rows back, recomputes only
the breadth series, and stores `etf_instruments`, `etf_bars`, `sessions_compared`, the largest
`pct_above_dma` difference on any session, and — the number that actually matters —
**`gate_verdicts_changed`**, because the gate decides whether the book may enter at all and one
flipped verdict is a different trade where 0.01 of a point is not.

**The set added back is not `etf_instrument_ids`.** That helper reads membership of the `etf`
universe alone; `load_universe` *also* refuses a symbol ending `BEES`/`ETF`/`IETF` and a name
carrying the word, and on the snapshot this ran against the `etf` index has **no members at all**,
so those two patterns are the whole of the exclusion. `excluded_etf_ids` takes `04` §1.1/§1.2's
admitted set minus `load_universe`'s answer, which is exactly what was taken out.

**Measured: 310 instruments, 1,239 bars over 2,393 sessions, largest difference 0.0000 of a point,
0 gate verdicts changed.** Zero again — and the honest reading is that **this snapshot is as sparse
in ETFs as the export was** (1,239 bars is half a bar per instrument per *year*), not that the
question has been answered for a plant that carries them properly. The deep backfill running
against production the same evening will change the denominator of that sentence. The measurement
is on by default and stored on every run, so the day it stops being zero the row says so without
anybody remembering to ask.

**Rejected.** (a) Asserting zero in a test — that is inheriting the assumption with a test around
it, which is worse than inheriting it. (b) Skipping the pass because it costs a second indicator
run — it costs about a third of a 35-second run.

**Reversal.** `measure_etf_denominator=False`.

---

## TW9.5 — A number the run could not produce is an **absent key**, never a JSON null · ⚠ UNREVIEWED

**Context.** TW8's card reads `stats` through `@/lib/twt/numbers`' `Figure`, whose whole contract
is that a missing value arrives with the reason there is none and never as a dash. It recognises
"missing" as `undefined`. A JSON `null` is not `undefined`: it reaches `percent()`, fails to parse,
and renders the string `"null%"` on a page about money.

**Taken.** `stats_payload` drops every key whose value is `None` before storing. A book with no
losing trade has no profit factor; a window that does not cross `is_oos_split` has no in-sample
half; a flat curve has no Sharpe. All three are real states and the card already has a sentence for
each of them.

**Rejected.** (a) Storing `null` and teaching the page to treat it as absent — two places would
then have to agree about what nothing looks like. (b) Storing `"0.00"` — a zero standing in for an
unknown, on a page whose subject is a resting stop, is the failure the `Figure` rule exists for.

**Reversal.** One dict comprehension.

---

## TW9.6 — Two books, not VBT-1's three, and every percentage leaves as a decimal string · ⚠ UNREVIEWED

**Context.** `baskfy_worker.tasks.vbt_backtest` runs three books over one detection pass — `full`,
`gate_off` and `raw_scan` — and writes its statistics as floats.

**Taken.** Two books. `raw_scan` is VBT-1's ablation of its six trend filters; **TWT-1 has none**
(`01` §4 measured them and they make it worse, which is why `04` §3 names no field for one), so a
third book would be the same run twice under two names. `gate_off` stays, because it is the only
argument for the breadth gate and `05` §3 gives it a cell nothing else fills — measured here at
**14.91 % against the gated 22.17 %**, a gate worth 7.3 points on the plant's bars where the study
measured 3.7.

And every percentage is stored as a **decimal string of two places** rather than a float, which is
where `tw_backtest_run` differs from `vb_backtest_run` on purpose: `@/lib/twt/numbers`' rule is
that a rate converts and rounds exactly once, on the page, from a decimal string. A float in the
JSONB would be rounded a second time by whatever read it — the 11 Sep portfolio bug, one layer
down. `Drift.to_json` follows the same rule, so `baskfy_core.twt.drift` is not a copy of
`baskfy_core.vbt.drift` even though it answers the same question.

**Rejected.** Making `vb_` match — VBT-1's page and tests read floats today and Track C §8 says
that tree is read, never edited, during this run.

**Reversal.** `_pct` is one function.

---

## TW6.1 — The plan is offered the `SCAN_ONLY` rows, so the liquidity skip is recorded · ⚠ UNREVIEWED

**Context.** `04` §10.1 lists `BELOW_LIQUIDITY_FLOOR` among the plan's skips, in order, and `03`
§7 check-constrains `tw_plan_skip.reason` to it. But VBT-1's evening — the module this one is
shaped after — drops its `SCAN_ONLY` rows before building ("`SCAN_ONLY` rows are not candidates"),
and a skip the planner never sees is a skip that cannot be recorded.

**Taken.** `twt_evening.candidates_for` selects **every** `tw_signal_daily` row for the session,
both `SIGNAL` and `SCAN_ONLY`, and lets `build_entries` apply the floor itself. A name the floor
rejects therefore leaves a `tw_plan_skip` row saying so, with the floor's value in its detail.

**Why.** The ₹5 crore floor is this pack's one deliberate departure from the research's ₹2 crore
(TW0.3), and the funnel *is* the argument for it. A plan that silently dropped the names the floor
rejected could never show a person what the change costs — and "a plan is not honest without its
skips" is the sentence `04` §10.1 opens with.

**Rejected.** Filtering in SQL and counting the rejects into the step's `detail`: a number in a
log is not a row on the page, and `05` §2 renders the skips with their reasons in words.

**Reversal.** Add `TwSignalDaily.state == SignalState.SIGNAL.value` to `candidates_for`'s `where`.
One line; the plan then has no `BELOW_LIQUIDITY_FLOOR` rows and the constraint becomes decorative.

---

## TW6.2 — `TWT_EVENING` is an alert, because `05` specifies no email and a second mailer is a second place to hide a failure · ⚠ UNREVIEWED

**Context.** `06` § TW6 asks for "`AlertName.TWT_EVENING` email per `05`". `05` has four sections
and none of them is an email: §1 is the web hub, §2 the desk page, §3 the backtest card and §4
what the pages must never do. The criterion names a document that does not carry the thing.

**Taken.** `TWT_EVENING` is a real `AlertName`, raised by the evening job through
`baskfy_worker.alerts.dispatch` with the plan's gate, its counts, every line and every skip. That
mechanism already writes the log line, the Sentry breadcrumb and **the email** to
`BASKFY_OPS_ALERT_EMAIL`. Severity is `WARNING`, because the evening plan is not a failure and a
`CRITICAL` that fires every weekday teaches a person to filter it.

**Why not a template.** The swing book's EOD mail is a `Mailer`, a `Message` and a rendered
digest — a second address, a second transport and a second place for a delivery failure to hide.
`05` specifies neither the template nor its contents, so building one would be inventing a
contract rather than meeting one. The charter's precedence puts the literal wording of an
acceptance criterion lowest, and the Goal it is a proxy for — "the desk plan is delivered" — is met.

**Rejected.** (a) Skipping the notification entirely: the runbook's daily routine says "21:05 the
evening plan and the `TWT_EVENING` email", and a runbook that names a message nobody sends is
worse than one that says nothing. (b) Reusing `swing_eod`'s template with TWT words in it: the
swing digest's shape (flags, EPs, exposure level) has no TWT meaning.

**Reversal.** Write `twt_eod(address, digest)` beside `swing_eod` and call it from
`_raise_evening_alert`; the alert can stay beside it or go.

---

## TW6.3 — The desk spends the half-size countdown in SQL, because it cannot call TW5's function at all · ⚠ UNREVIEWED

**Context.** TW5 wrote `baskfy_api.twt_sleeve.count_first_live_entry` and TW6's brief says to call
it rather than reimplement it. `04` §6.4 spends the countdown **on a fill**, and on this sleeve the
fill happens inside the desk's confirm — in `kite-momentum-rebalancer`, a different process with a
different virtualenv and **no async Postgres driver**: `import asyncpg` fails there, so an
`AsyncSession` cannot be constructed and the function cannot be called even indirectly.

**Taken.** The *sizing* half is genuinely shared — `app/twt_execute.py` imports
`baskfy_core.twt.sizing.first_live_multiplier`, the same function TW5's `slot_multiplier` calls, so
the number a line is halved by has exactly one implementation. The *spending* half is
`PgTwtStore.count_first_live_entry`, which reproduces TW5.3's three refusals against the same two
columns (`tw_position.half_size`, `tw_config.first_live_entries_left`) and writes the same
`tw_config_audit` row with `changed_by = "twt-fill"`.
`tests/test_twt_desk.py::test_the_countdown_spends_once_and_never_on_a_simulated_fill` asserts all
three refusals.

**Why this is acceptable here and not in general.** It is the argument `vbt_desk` already makes in
writing about VB12's rescan constants: the desk cannot import the worker, so the numbers are
copied and a test asserts the copies agree. What is *not* copied is arithmetic — there is no second
implementation of the multiplier, the equity, the stop or the tick.

**Rejected.** (a) Adding `asyncpg` and SQLAlchemy-async to the desk's requirements to call one
function: a new dependency on the process that places orders, against house rule 1, to save
thirty lines. (b) Leaving the countdown to a nightly job: `04` §6.4 says "by the session that
filled it", and a job that counted yesterday's fills would let a second live entry be sized full
because the first had not been counted yet.

**Reversal.** If the desk ever gains an async Postgres session, delete the store method and call
TW5's function; the signature is already the one this method's arguments are named after.

---

## TW6.4 — `/twt/execute` refuses a `SELL_AT_OPEN` line outright · ⚠ UNREVIEWED

**Context.** `TwLineKind` carries `SELL_AT_OPEN` so a person can be given a line for a `MANUAL`
exit without a migration (`03` §7), and `04` §10.2 says **no TWT rule ever emits one** — TW10
asserts the absence. Nothing said what the confirm route should do if one appeared anyway.

**Taken.** `EXECUTABLE_KINDS` is the three kinds the planner can emit; a `SELL_AT_OPEN` is a
**400** naming the reason ("this sleeve has no end-of-day sell rule and the GTT is the exit"). An
import-time `assert` pins the absence so the set cannot grow by accident.

**Why.** A route that could execute the kind would *be* an end-of-day sell rule, however
carefully nobody planned one — and the shape this module copied (VBT-1, the swing book) has such a
rule, which is exactly how rules get imported by accident. The charter breaks ties toward the
stricter boundary when nothing in force changes, and nothing does: no TWT plan contains the kind.

**Rejected.** Implementing a sell path for the `MANUAL` case. A manual exit is a person selling in
the Kite app with their eyes on the chart (FIRST-LIVE-MORNING §9.2 step 4); giving the desk a sell
button for a strategy whose only exit is the GTT is a second exit rule nobody measured.

**Reversal.** Add `SELL_AT_OPEN` to `EXECUTABLE_KINDS`, delete the assert, and write `_sell_at_open`
against `tw_position.quantity_open` — the swing book's is 60 lines and the shape is known.

---

## TW6.5 — The TWT stop band and GTT cushion are config fields, not desk env knobs · ⚠ UNREVIEWED

**Context.** The swing book and VBT-1 each carry their band and their limit fraction as
`BASKFY_*` variables in the desk's `app/config.py`, *and* the engine carries the same numbers.
`04` §10.6 and §10.7 put TWT's in `baskfy_core.twt.config.ExitConfig`
(`gtt_band_min_pct`/`gtt_band_max_pct` and `gtt_limit_fraction`).

**Taken.** `app/twt_execute.py` reads all three off `DEFAULT_TWT_CONFIG.exits`. `app/config.py`
gains exactly one TWT entry, `BASKFY_TWT_EXECUTION_ENABLED`, and `.env.example` says in so many
words why the other three are not there.

**Why.** The band is checked against a trigger the engine computed. Two copies of "0.30" agree
until the day somebody edits one, and the failure mode is a gateway refusing this sleeve's own
stop — which would make non-negotiable 4 unsatisfiable on a live line. One number, read where the
arithmetic lives.

**Rejected.** Copying the swing/VBT shape for symmetry. Symmetry is not a reason to create a
second source of truth for a number that decides whether a stop can be armed.

**Reversal.** Add the three `BASKFY_TWT_*` variables to `app/config.py` and `.env.example` and
read them in `twt_stop_band` / `twt_limit_fraction`; both functions are three lines.

---

## TW6.6 — A plan is expired **at** its expiry, not after it · ⚠ UNREVIEWED

**Context.** VBT-1's `/vbt/execute` refuses a plan when `now > expires_at`. `/twt/halt` must make
every live plan unconfirmable, and the obvious implementation stamps each one's `expires_at` with
the moment of the halt — which a strict `>` would leave confirmable for the microsecond it takes
to answer.

**Taken.** `/twt/execute` compares `now >= expires_at`. A plan built at 09:05 with a thirty-minute
life is dead at 09:35:00.000, and a plan the halt stamped at 09:20:00.000 is dead at 09:20:00.000.

**Why.** The safe direction, and it makes the halt's second behaviour exact rather than nearly
exact. "Expires in thirty minutes" is what the runbook tells a person, and at minute thirty the
honest answer is that it has.

**Rejected.** Stamping `now - 1s` in `expire_plans` and leaving the comparison alone — a second
that exists only to paper over an off-by-one, and a reader would have to find the subtraction to
understand the rule.

**Reversal.** One character in `_validate`.

---

## TW6.7 — The desk's own 15:15 sweep logs the alert name; TW7's tool raises it · ⚠ UNREVIEWED

**Context.** `06` § TW6 says the sweep "raises `TWT_GTT_MISSING_AT_1515` for what is still naked
afterwards". `AlertName` and `dispatch` live in `baskfy_worker`, which the desk process cannot
import — the same venv boundary as TW6.3.

**Taken.** `sweep_naked` logs at `error` with the alert's exact name and the symbols, which is
what `app/swing_clock.py` already does for `SWING_GTT_MISSING_AT_1515`, and writes the count into
`tw_session.naked_at_1515` where the page's red band reads it. The four `TWT_*` names exist in
`baskfy_worker.alerts.AlertName` with `docs/runbooks/09-twt-morning.md` behind them, and
**`tools/twt/sweep.py` (TW7) raises the real alert from the tree that can**.

**Why.** Two copies of the sweep would be worse than one copy and a log line: the count that
matters is in the database either way, and the desk's route exists so a person can check it from a
phone rather than so a robot can page itself.

**Rejected.** A webhook from the desk to the worker's alert sink — a new network dependency on the
process that places orders, for a message.

**Reversal.** None needed; TW7 owns `tools/twt/sweep.py` and reads the same rows.

---

## TW10.1 — A parent gate re-runs its child's checks, and the tool for that lives in this repo · ⚠ UNREVIEWED

**Context.** `gates/twt-root.md` R6, R7, R9 and R10 each ran the unlazy checker against a module's
gate file with `EXPECT: /0 unchecked/`. The checker never prints that string — it prints
`ALL MET (11 met)` or `UNMET: 3` — so none of the four could have passed. Underneath the wrong
string sat a wrong question: the checker only re-runs gates it already believes unmet, so against a
finished file it executes nothing and reports that the **file** is complete. That is a fact about
the ledger, not about the module still being green, and a parent row exists to establish the
second.

**Taken.** `tools/gates/rerun.py` re-executes every `CHECK:` in a named gate file and verifies each
against its own `EXPECT:`, printing one summary line (`gates/twt-6.md: 11/11 checks re-run,
0 failed`) that a parent's EXPECT matches. The four rows call it. It duplicates the checker's
parsing and its EXPECT semantics deliberately — including the `i` and `m` regex flags, because
`gate-check.mjs` compiles the same literal with `new RegExp(body, flags)` and the two must not
disagree about whether a gate passed.

**It also sidesteps a bug in the checker**, which is in the skill and not in this repo:
`args.filter((a, i) => !a.startsWith("--") && i !== tIdx + 1)` with no `--timeout` present has
`tIdx === -1`, so the predicate drops index 0 — the only argument there is. A lone file argument
falls back to every `gates/*.md` in the tree, and since `gates/twt-root.md` invokes the checker, it
recurses without bound — five nested levels within two minutes, on 12 Sep 2026, before it was
killed. `--status <file>` and `--timeout N <file>` both dodge it. **Reported rather than patched:** editing a globally installed skill from inside a repo
run is a change nobody reviewing this commit would see.

**Rejected.** Keeping `gate-check.mjs` and correcting only the EXPECT string. It would have made
the rows *pass* without making them *check*: four green rows asserting that four files look
finished, which is the decoration this run has spent two commits removing.

**Reversal.** Delete `tools/gates/rerun.py` and put the checker invocation back, with
`--timeout N` before the file so the argv bug does not bite. The rows' Goal is unchanged either
way; only the strength of the evidence moves.

---

## TW10.2 — A gate cannot be its own evidence, so the exclusion is printed rather than hidden · ⚠ UNREVIEWED

**Context.** `gates/twt-10.md` G8 asserts that every gate file in this run is fully checked with
evidence. Its own `EVIDENCE:` line reads `pending` until it passes, so when it scanned the
directory it counted itself and failed — for a reason with nothing to do with what it tests. G8 had
already been repaired once for a neighbouring fault (its CHECK line contained the literal string it
grepped for); this is the same shape one layer down.

**Taken.** `tools/gates/ledger.py` reports which gate files are incomplete and takes
`--exclude FILE:GATE_ID`. The exclusion is **printed in the summary line** —
`14 files scanned, 142 gates, 0 incomplete (excluded as self-referential: gates/twt-10.md G8)` — so
a reader sees exactly what is not being asserted. It also reads an **indented** `ABANDON:` line,
which is how this repo writes them (`gates/twt-0.md` line 79) and which `gate-check.mjs`, anchoring
at column 0, does not see; a scoped gate reads there as merely unchecked.

**Rejected.** Two alternatives. Subtracting one from the file's own pending count — arithmetic that
is right today and silently wrong the moment a second gate pends. And moving G8 into a file of its
own — which makes the self-reference invisible rather than absent, since the new file is still one
of the ones being scanned.

**Reversal.** Drop the `--exclude` argument and accept that G8 is asserted by the run's report
rather than by a gate. Nothing else depends on the tool.

---

## TW10.3 — The sleeve has no funding surface, and TW10 does not build one · ⚠ UNREVIEWED

**Context.** Re-reading `FIRST-LIVE-MORNING.md` command by command — because `02` §3 makes the
runbook a *condition* and a condition is not green because its gate file is — turned up that
**`tw_config.sleeve_capital_inr` cannot be set by anything**. `PATCH /api/v1/twt/config` has no
router (`twt_settings.py` exports `read_config`, `apply_patch` and `record_system_change` and
mounts none of them), there is no `me/twt` page, and `seed twt` takes no `--capital` flag although
`seed swing` has taken one since SW13. NEEDS-MAULIK T3 calls the capital "your keystroke" and there
is nowhere to type it.

Nothing here is *new* — §2.2's three commands have carried `[NOT YET REAL — TW3/TW8]` since TW10a
wrote them. What was absent is the addition: three individually honest markers, and no line
anywhere saying that together they mean the sleeve cannot be funded.

**Taken.** Record it in three places (`NEEDS-MAULIK.md` T3, `TW-FINAL-REPORT.md` *Not done*, and
this entry) with the two ways to close it, and **build neither**.

**Why not build it.** The smaller option — `set_twt_sleeve` beside `set_swing_sleeve` plus a
`--capital` flag — is maybe forty lines and audits correctly, and the temptation to add it while
the file is open is the whole reason to write this down instead. Three reasons not to. This run's
brief is that it **never funds the sleeve**, and shipping the funding mechanism as an unasked
addendum to the safety module is the kind of scope creep that arrives inside a commit nobody
expected it in. The root `CLAUDE.md` names `tw_config.sleeve_capital_inr` as a thing an agent
never sets, and building its only setter at the end of an unrelated module reads against the grain
of that even where it does not break it. And **TW3/TW8 own this surface** by the runbook's own
markers; a third place to set the capital, built by a fourth module, is how a sleeve ends up with
two answers to what its capital is.

**Rejected.** Building the CLI flag quietly. Also rejected: leaving it in the runbook's markers
only, which is the state that let three honest tags add up to an unnoticed blocker.

**Reversal.** Not needed — nothing was built. Closing it is a later module's `set_twt_sleeve`, or
the TW3/TW8 route the runbook already assumes.

---

## TW11.1 — The funding surface TW10.3 declined, built once Maulik had seen the choice · ⚠ UNREVIEWED

**Context.** TW10.3 found that `NEEDS-MAULIK.md` T3 called the sleeve's capital "your keystroke"
while there was nowhere to type it, and deliberately did not build the surface: *"building the
funding surface at the end of an unrelated module — without you having seen the choice — is exactly
the kind of scope the autonomy charter says to hand back instead."* That was the right call and it
named its own reversal: *"Closing it is a later module's `set_twt_sleeve`."*

**What changed is the one thing TW10.3 was waiting on.** On 12 Sep 2026, under `/unlazy` on
`REMAINING.md` §2, Maulik was shown the choice and asked for it. So this is TW10.3's reversal
taken, not a decision re-opened.

**Decision.** `set_twt_sleeve` in `seed.py`, beside `set_swing_sleeve` and in its shape, plus
`--capital` widened from `swing` to `swing|twt`. It writes through `twt_settings.apply_patch` — the
same function the settings form would use — so the engine's bounds are checked first, the server
ceilings second, and every field that moves leaves a `tw_config_audit` row with an author. The
value is quantised to the column's 2 dp before the patch is compared, or `2500000` against a stored
`2500000.00` would audit a change on every deploy (house rules 7 and 8).

**`--risk` was NOT widened.** `tw_config` has no risk-per-trade column: this book sizes by slot
(`max_position_pct`), not by stop distance. A flag accepted and then ignored is the failure
`TwtConfigPatch`'s `extra="forbid"` exists to prevent, so the parser refuses it and says so.

**The ₹0 is untouched and is now asserted twice.** Creation still writes an explicit `Decimal("0")`
and only an explicit `--capital` moves it; `TestTheZeroSurvives` reads the seeder's own source and
fails if anyone ever wires the funding call into the `twt` branch unconditionally. No capital was
set by this work. `gates/twt-root.md` R11 still reads `flag_true=0 live_orders=0 capital_seed=1`.

**Rejected.** The `PATCH /api/v1/twt/config` route and the `me/twt` page. They are the TW3/TW8
shape the runbook assumes and they remain unbuilt: T3 asks for a keystroke on a box at 09:05, and
the CLI is the surface that exists there. The route is worth building and is not blocking.
Also rejected: the raw `UPDATE` on `tw_config`, which works today and is the worse option for the
one reason that matters — no author, no trail.

**Reversal.** Delete `set_twt_sleeve`, narrow the two `parser.error` branches back to `swing`, and
drop `services/api/tests/test_seed_twt_sleeve.py`. Nothing else reads it. A capital already written
through it stays written, with its audit row, which is the point.

---

## TW11.2 — The sleeve's clock gains the evening and the morning, and must never gain the sweep · ⚠ UNREVIEWED

**Context.** `baskfy.twt.evening` and `baskfy.twt.morning` were registered tasks with no Beat entry
from TW6 to 12 Sep 2026, while `twt-detect` had had one at 21:00 since TW4 and VBT-1 had both.
`docs/twt/FIRST-LIVE-MORNING.md` tells a person to open the desk and read a plan that nothing was
building; `make twt-plan` was the only thing that made one.

**And `REMAINING.md` §2 was wrong about it in a way worth recording**, because it is the same fault
this pack has now logged thirty-odd times. It said *"Nothing schedules the evening, the morning or
the sweep. **No Beat entry, no TWT entry in the desk's clock.**"* The first sentence was true. The
second was not: `twt-detect` has been in `BEAT_SCHEDULE` since TW4, with a comment explaining its
time. A summary that rounds "two of the three are missing" up to "there is nothing here" is how a
gap gets fixed twice or not at all.

**Decision.** `twt-evening` at 21:20 and `twt-morning` at 09:05, both Mon–Fri, both on the compute
queue. 21:20 rather than the VBT pair's 21:15 for two reasons and both are ordering: the plan is
built from the session's signals and the session's gate, so an evening before `twt-detect` at 21:00
would plan against yesterday's tape; and the detector densifies 260 sessions over the whole cash
universe, so it should not share the compute queue with `vbt-evening`. 09:05 is after `vbt-morning`
at 09:00, after the login window, and before the 09:15 open — a plan confirmed at 09:20 must not be
one built last night, because the desk expires plans in thirty minutes.

**This is safe only because of what those two jobs are.** `tasks/twt_evening.py`, first line of its
docstring: *"Nothing here places an order. Every line is `PROPOSED`; a person confirms it on the
desk."* Scheduling a planner is not auto-execution; scheduling anything that reaches
`OrderGateway` would be.

**Refused, and this is the load-bearing half of the entry: the 15:15 sweep is not scheduled.** The
sweep re-arms GTT stops, so it is order flow, and a Beat entry for it would be the desk placing
orders on a timer. The root `CLAUDE.md`'s first non-negotiable allows exactly one named
auto-execute exception, it is the *swing* sleeve's, it is flagged, and it is Maulik's — *"An agent
may not widen this exception, add a second one, or default the flag to true."* The sweep stays
`POST /twt/sweep` and `tools/twt/sweep.py`, which is what the runbook §9 already tells a person to
run. `test_the_sweep_is_not_on_a_timer` makes that a check rather than a comment, asserted twice:
no TWT entry may be named for a sweep, and none may point at a task whose name contains one.

**Reversal.** Delete the two entries and `services/worker/tests/test_twt_beat.py`. The tasks remain
registered and `make twt-plan` remains what it was, so nothing else changes.

---

## TW11.3 — Eleven root rows, nine broken checks, and two real gaps · ⚠ UNREVIEWED

**Context.** `gates/twt-root.md`'s preamble diagnosed a real bug — `EXPECT: /0 unchecked/` against
a checker that prints `ALL MET` or `UNMET: n` — repaired four rows, and left **seven more instances
of the identical bug** in the same file, all marked green. R0–R5 and R8. Re-running them was the
only way to find that out.

**What re-running the whole ledger turned up.** **Nineteen** failing checks underneath the eleven
rows — five in twt-0, two in twt-1, five in twt-2, four in its harness, one each in twt-3, twt-4 and
twt-8. Eighteen needed the check itself repaired and each carries a dated note on its own gate
rather than here; the nineteenth, twt-8 G9, needed nothing repaired but the repository's lint. The
classes:

| Class | Where |
|---|---|
| EXPECT spanning two or three lines with `.*`, which does not cross a newline | twt-2 G15, twt-2-harness G12, G14 |
| Anchored EXPECT with no `m` flag, so `$` cannot match | twt-0 G0.8, G0.9, twt-2 G13 |
| Multi-line `python -c` in a CHECK, which `sh -c` receives as one line and rejects | twt-2 G6 |
| A grep window that drifted as its command's output grew | twt-1 G12, twt-3 G10, twt-2 G5 |
| A filter that had not kept up with the module | twt-1 G9, twt-2 G3 (the study's Sharpe read as money), twt-4 G2 (a signal-delete guard globbed onto TW6's plan delete) |
| A point-in-time assertion that correctly inverted | twt-2-harness G12 (`seam: NOT READY` — TW2 closed the seam), twt-0 G0.4 (migration 0041 was free; TW3 took it) |

**Two were not check bugs at all, and they are the reason this was worth doing.**

1. **`06`'s TW6a had no `**Goal:**` line** — twelve module headings, twelve ACs, eleven goals. `06`'s
   own convention is that every module carries both. Written.
2. **`make lint` was red across the whole repository**, on one TypeScript error and one ESLint error
   in the UI tree's uncommitted test files. Three TWT gates depend on that command and had been
   recorded green before the breakage landed. Fixed type-only; `REMAINING.md` §4 records it under
   the tree that owns those files, and says that tree may overrule it.

**Decision.** Repair every check to test the thing it names, re-run all eleven rows through
`tools/gates/rerun.py`, and write the reason into the gate beside each repair rather than into this
file. A repair with no reason beside it is indistinguishable next month from someone loosening a
gate to make a module pass.

**Rejected.** Marking the seven rows unmet and handing them back — the checks were wrong, not the
modules, and every module proved green once its checks could run. Also rejected: leaving the two
real gaps for their own sessions. A missing Goal is one sentence, and a red repo-wide lint gate
makes every future gate meaningless.

**Reversal.** `git diff` on the gate files. Every repair is one CHECK or EXPECT line with a dated
note above it; nothing about any module's code changed to make a gate pass.

---

## TW11.4 — A gate that emptied the developer's database, and had since TW3 · ⚠ UNREVIEWED

**Context.** `gates/twt-3.md` G2 proved migration `0041_twt` round-trips by running
`make migrate && make downgrade && make migrate`. No `BASKFY_DATABASE_URL` is set in that CHECK, and
the default in `services/api/src/baskfy_api/settings.py:34` is `localhost:5433/baskfy` — the
developer's own database, the one `DESK_DATABASE_URL` and `SCREENER_DATABASE_URL` also name. The
Makefile's `downgrade` target is `alembic downgrade **base**`, not `-1`.

**So the gate dropped every table in that database and recreated it empty, every time it ran** —
including on 11 Sep when TW3 first recorded it green, and again on 12 Sep when the ledger was
re-run. The `tw_config` row the database was holding (₹25,00,000, `updated_by = test`, no audit row)
went with it. The database was close to an empty schema by size beforehand and the plant's bars live
in other databases, so the loss is probably that one row — **but the contents were not recorded
first, so that is an estimate and is written here as one.**

**The EXPECT was weak in the same direction.** `/Running upgrade|Target database is not up to
date|head/` matches a single successful `alembic upgrade`. A run in which the downgrade half never
executed would have passed — and the downgrade is the only thing the gate exists to test.

**Decision.** Round-trip a throwaway. `DROP DATABASE IF EXISTS baskfy_migrate_check; CREATE
DATABASE …`, point `BASKFY_DATABASE_URL` at it for the duration, and assert the **end state** —
`version=0041_twt tw_tables=13` — rather than a word in a log. This is the pattern `gates/twt-10.md`
G6's drill already used for the same reason; G2 simply never adopted it.

**Rejected.** Setting `BASKFY_DATABASE_URL` to `baskfy_test` instead: that is where the suites live,
so a gate that downgrades it to base mid-run breaks every concurrently running test — a smaller
version of the same fault. Also rejected: leaving the gate and documenting the hazard. A destructive
command in a file whose entire purpose is to be re-run is not a documentation problem.

**The general lesson, and it is bigger than this gate.** A ledger is re-run by definition. Any CHECK
that writes must write somewhere disposable, and any CHECK that takes a database URL from a default
is pointing at whatever the person's environment says — which on a deploy box is not a test
database. **This is the only destructive CHECK found in the TWT pack**; `docker exec … DROP DATABASE
baskfy_drill` in twt-10 G6 and the new `baskfy_migrate_check` both name a scratch database
explicitly, which is the rule the rest of the pack already followed.

**Reversal.** `git diff gates/twt-3.md`. Reverting restores a gate that empties your database.

---

## TW12.1 — TWT had no scan-run table, and gets `tw_scan_run` rather than reusing a row · ⚠ UNREVIEWED

**Context.** `PLAN-SCAN-SYNC.md` asks for "Scan now" on the TWT page, in the shape SW15 built for
the swing book: `POST /twt/scan` answering **202** queued / **409** in flight / **429** rate
limited, and `GET /twt/scan/{run_id}` for the poll. Both older sleeves have a table behind that
button — the swing book's `sw_scan_run` (`0033_swing_scan_now`) and VBT-1's `vb_scan_run`
(`0040_vbt_scan_run`). **TWT had neither**, and the plan left the choice to this leaf: add a table
by migration, or justify reusing an existing row shape.

**Decision.** Add `tw_scan_run` by migration **`0042_twt_scan_run`** (revising `0041_twt`, the next
free number; `0043_split_holding_reason` has since chained onto it and the head is single). Twelve
columns, three check constraints, one index on `(user_id, requested_at)` — the query both refusals
make. Documented as `docs/twt/03` §11; the sleeve now has fourteen tables and
`test_twt_schema.py`'s count says so with the reason beside it.

**Rejected: reuse `tw_session`.** It is the closest existing row — one per session the system ran,
already carrying counters. It is also the *record of a session*, not of a request: it is keyed
`(user_id, session_date)`, so two presses on the same day would collide, a `FAILED` scan would
have to be written onto a row the evening job reads as its calendar, and "the scan failed" would
become indistinguishable from "the session failed". The evening job reads that table to decide
what to plan from. Corrupting it with request state would be a money bug reachable from a button.

**Rejected: reuse `tw_backtest_run`.** It has the right *shape* — `started_at`, `finished_at`,
`params`, `error` — and entirely the wrong meaning. A page showing "your last run" would mix scans
and backtests, and `03` §9's index is `(user_id, source, finished_at)` for "the latest finished run
per source", which a scan would pollute.

**Rejected: no table at all — publish the Celery task and answer 202 blind.** It is the smallest
change and it loses the two refusals, which is most of the feature: both are answered *from the
table* rather than from Redis, deliberately, so they hold on a box with no cache and are testable
against the database alone (the argument SW15.1 made and VB12 repeated). It also loses the poll,
and a button with no poll is a button that looks broken for the two minutes the detector runs.

**Why it is narrower than `sw_scan_run`.** See TW12.2: no `provisional` column.

**Why `source` admits `web` where `vb_scan_run`'s admits only `desk` and `cli`.** See TW12.3: the
`/twt` hub is getting this button, and a row that cannot say which button was pressed is a row that
cannot be audited afterwards.

**Reversal.** `alembic downgrade 0041_twt` drops the table and its index and nothing else —
asserted against a throwaway database in `gates/twt-scan-now.md` G2a (`version=0041_twt
tw_tables=13 scan=0 idx=0`). Then delete `models/twt.py`'s `TwScanRun`, `baskfy_api/twt_scan.py`,
`baskfy_api/routers/twt.py`, `baskfy_worker/tasks/twt_scan.py`, the two routes in `app/twt_desk.py`
and the four test files. Nothing else reads any of it. **No row of any existing table changes
shape**, so a reversal loses only the scan history.

---

## TW12.2 — The scan runs the detector that exists, and there is **no** provisional intraday path · ⚠ UNREVIEWED

**Context.** SW15's "Scan now" does something clever: during the session it builds a bar per liquid
name out of live Kite quotes and detects on *today so far*, labelling every row `provisional`. It
is the right answer for the swing book, whose setups — a base, a pivot — are readable off a bar
still being formed. The obvious way to build this button for TWT is to copy that.

**Decision.** Do not. `baskfy.twt.scan` detects the **latest published session** and calls
`baskfy_worker.tasks.twt.detect_session` — the same function `baskfy.twt.detect` calls — with
`force=True`. There is no provisional path, no quote read, no `provisional` column on
`tw_scan_run`, and no `provisional` field on the API payload.

**Why.** `docs/twt/04` §2 measures three *weekly* ranges that have closed, against a monthly low,
with a sessions-out count over closed sessions. A bar built at 13:42 would change every one of
those numbers and make none of them truer — the week is not over. Worse, it would make them
*actionable-looking*: the whole strategy is "this name has gone quiet for three weeks", and a
half-formed week is exactly the input that makes a noisy name look quiet. VBT-1 reached the same
refusal from different arithmetic (`vb_scan_run` has no `provisional` column either, because three
of its five Chartink lines are closed-day facts). Two sleeves, one conclusion, and the swing book
is the exception rather than the pattern.

**Why `force=True`, which the nightly does not pass.** The nightly's rule is "skip a session that
already has a breadth row" (TW4.3, and it is a good rule — this strategy signals about eighteen
times a year, so "no signals" is the ordinary state of a session that ran perfectly). A person
pressing **Scan now** is asking for precisely the session that rule skips: the night a threshold
changed, or the night the quality gate refused the day. Without `force` the button would answer
`{"skipped": "already detected"}` and look broken. The write underneath is an upsert, so the
re-run is still idempotent (house rule 7) — asserted by counting `tw_breadth_daily` rows after two
presses.

**Rejected: a second detector inside `twt_scan.py`.** A second implementation of the tight-state
rule would drift from the first the moment a threshold moved, and the two would then disagree about
the same session on the same page. `gates/twt-scan-now.md` G9 and three tests assert the module
contains no detection of its own.

**Rejected: detect "today" from the exchange calendar.** VB13.4 is the recorded cost of that: the
calendar calls Friday a trading day from midnight, Friday's bars do not exist until the chain
publishes that evening, and a press at 14:14 faithfully detected a session the bars had never heard
of. `latest_published_session` asks `pipeline_run.data_version` instead — the product's own answer
to "what is the latest session", the one the freshness pill reads.

**Reversal.** Adding a provisional path later is additive: a `provisional` boolean column, a quote
source on the task, and the swing book's `provisional_bars` for a model. Nothing here forecloses
it. What it would need first is an answer to "what does a three-week contraction mean two days into
the third week", and `04` does not have one.

---

## TW12.3 — The `/twt` hub gains its first write, and it is the scan · ⚠ UNREVIEWED

**Context.** `docs/twt/02` Track A says the web app's `/twt` hub is *"read-only — the same rule as
`/baskets`, `/swing` and `/vbt`: every mutation on it is a 405 except notes and dismissals, which
change no money."* Track C §4 says *"`apps/web` gets no route under `/twt` that can reach the
gateway."* Those are two different sentences and only the second is absolute.

`PLAN-SCAN-SYNC.md`'s contract says the web action "posts to the desk through the same path
`swing/actions.ts::scanNow` uses" — and that path is `swingWrite` → `${API}/api/v1/swing/scan`, the
**API**, not the desk console. So Leaf 5's button needs `POST /api/v1/twt/scan` to exist. Before
this leaf there was no `/twt` router in the API at all: `apps/web/src/lib/twt/fetch.ts` asks for
`/twt/today` and `/twt/backtest` and gets `null`, by design (TW8 built the page ahead of its data).

**Decision.** Add `services/api/src/baskfy_api/routers/twt.py` with exactly two routes — the scan
and its poll — and register it. The hub's rule becomes "read-only except one money-free write",
which is the swing book's rule and was already Track A's own carve-out for writes that change no
money. **Recorded here rather than assumed**, because widening a Track A sentence on this sleeve is
not a thing to do silently.

**What makes the write defensible, and each of these is asserted.** It writes **one row in one
table** (`tw_scan_run`) and publishes **one task name** (`baskfy.twt.scan`). The module names no
broker, no execution package, and does not so much as *read* `twt_execution_enabled` — a scan has
no business branching on it. Every route resolves the sole tenant rather than trusting the
principal. `services/api/tests/test_twt_readonly.py` asserts all of it over the source and over the
OpenAPI document, including that the one mutating route is **exactly** `POST /twt/scan` and not a
superset.

**Track C §4 is untouched.** There is no `POST /twt/execute` in the API and this leaf did not make
one easier to add: confirming a line is still the desk's, behind Maulik's own login.

**Rejected: build only the desk route and let Leaf 5 point the web action at the desk console.**
The desk is on a different host, behind basic auth and an `Origin` check meant for a browser Maulik
is sitting at. Pointing the public web app's server action at it would put the operator console in
the web app's trust boundary for the sake of one button — the exact coupling `05` §2 keeps apart.

**Rejected: serve `/twt/today` and `/twt/backtest` here too, since the router had to exist.** They
are a different module's work with a different acceptance, and a router that grows read surfaces on
the way to a button is a router nobody reviewed. The path count in `test_twt_readonly.py` is
asserted at **two**, so whoever serves them reads that file first.

**A consequence for Leaf 5, stated so it is not a surprise.** `apps/web/src/app/(app)/twt/` has no
`actions.ts` and `src/lib/twt/fetch.ts` says in its own docstring that it has no write helper and
is not getting one; `__tests__/read-only.test.tsx` asserts it. Adding the button means changing
that test — which is the same "a new write is a deliberate act" property the desk's route
discovery gives (`test_twt_safety_properties.py`). This leaf did not change it, because the UI is
Leaf 5's column.

**Reversal.** Remove the `versioned.include_router(twt.router)` line in `app.py` and delete
`routers/twt.py`, `twt_scan.py` and their two test files. The desk's `POST /twt/scan` is
independent and would keep working.

---

## TW12.4 — The publisher is not called a sweep, because on this sleeve that word means the gateway · ⚠ UNREVIEWED

**Context.** TW12's publisher was first written as Beat entry `twt-scan-sweep` firing task
`baskfy.twt.scan_sweep`, copying `swing-scan-sweep` (SW15) and `vbt-rescan-sweep` (VB12) exactly.
It does one indexed `SELECT ... WHERE status = 'QUEUED' AND task_id IS NULL` and an
`apply_async`. It cannot reach a broker, and nothing about it is dangerous.

**`services/worker/tests/test_twt_beat.py::test_the_sweep_is_not_on_a_timer` failed it, and the
test was right.** Its assertion is that no TWT Beat entry may be *named* for a sweep and none may
point at a task whose name contains one, with the reason written above it: on this sleeve "sweep"
is `app.twt_execute.sweep_naked`, the 15:15 chore that re-arms GTT stops **through the gateway**,
and scheduling that would be a second auto-execute exception — which non-negotiable 1 says an agent
may not add. TW11.2 says the same thing in this file: *"The sleeve's clock gains the evening and
the morning, and must never gain the sweep."*

**Decision.** Rename, do not narrow. `baskfy.twt.scan_publish`, Beat entry `twt-scan-publish`,
`PUBLISH_LIMIT` rather than `SWEEP_LIMIT`, and the module's prose says "publisher" throughout. The
assertion in `test_twt_beat.py` is unchanged apart from a note recording that it caught this, and
one added line asserting the publisher is still *there* — so a future rename back to a sweep fails
twice, once on the word and once on the absence.

**Rejected: narrow the assertion to `sweep_naked` or to an allow-list.** It would have worked and
it is the wrong trade. The value of that test is that somebody grepping Beat for "twt sweep" at
3am gets nothing — and a `twt-scan-sweep` entry would give them a hit that looks exactly like the
thing they are afraid of. A safety check that has to be read carefully before it can be trusted is
a safety check that will be misread. Weakening it to accommodate a *cosmetic* naming symmetry with
two other sleeves is the clearest case of "never weaken a test to make a module pass" in this leaf.

**Rejected: keep the name and add an exemption comment.** Same objection, one indirection worse.

**The cost, stated honestly.** The three sleeves' publisher tasks are now named inconsistently:
`baskfy.swing.scan_sweep`, `baskfy.vbt.rescan_sweep`, `baskfy.twt.scan_publish`. That is a real
loss — a reader who learns one sleeve no longer guesses the third. It is the smaller loss.

**Reversal.** Rename back in `celery_tasks.py`, `celery_app.py` (routes and Beat),
`baskfy_api/queue.py` and `EXPECTED_TASKS` in `test_twt_safety_properties.py` — and you will then
have to weaken `test_the_sweep_is_not_on_a_timer`, which is the point at which this decision should
be re-read rather than reverted.

---

## The pack's own standing position on two things it was not asked

**There is no paper phase and this file does not re-open it.** Maulik decided it on 11 Sep 2026,
before the run started; `02` §3 is written to that decision. What the pack *does* record is the one
consequence worth naming: the ratchet will first run with money behind it, which is why TW7's
fill-day rule and TW10's sweep exist.

**The run never flips a flag and never sets a capital.** Root `CLAUDE.md` safety rails. Nothing in
this file is a reason to.

---

### TW11 — `/twt` gains one server action, "Scan now", and the read-only test becomes a census · ⚠ UNREVIEWED

**Context.** `PLAN-SCAN-SYNC.md` leaf 5: Maulik asked for the swing hub's "Scan now" button on the
three-weeks-tight and volume-breakout pages. Until 12 Sep 2026 this tree had **no server action at
all**, and `__tests__/read-only.test.tsx` asserted exactly that — `05` §1 permits two money-free
writes here (a note and a dismissal), TW8.7 declined both because `03` has no table to put a note
in, and "no action at all" was therefore the strongest true claim available.

**Decision.** Build the button, and replace that assertion with the swing hub's **census** rather
than deleting it. The file walk enumerates every export of every `use server` module under the
tree and the set must be exactly `["scanNow"]`; it pins the hub at one form and one submit button,
requires the action to go through one write helper, and asserts that helper's path type is a
**closed union of one literal** — `export type TwtWritePath = "/twt/scan";` — with POST as its only
method. The 405 assertion survives: no `route.ts` exists under the tree, and a server action is a
function reference the framework dispatches, not an address anybody can post to by guessing.

**Two extra guarantees this sleeve needs and the sibling does not.** The census also asserts that
the **write path** — the action plus its helper — can name neither this sleeve's capital nor its
execution switch. `02` §3 says those are Maulik's "in any circumstance", and the plan's hard rule 2
repeats it. The check is deliberately scoped to the write path rather than the whole tree, because
the half-size counter legitimately *reads* the stored switch in order to say that trading is off;
reading a setting is not setting one.

**Why a scan is allowed where an order is not.** A scan queues the detector: prices in, detection
rows out. `02` Track C §4 is unchanged, and `05` §2 still puts the click in the desk console.

**The status is mapped to a sentence, and the service's own `detail` is never rendered.** 202, 409
and 429 become "Scan started…", "A scan is already running…", "A scan has just been run. The next
one can start in about a minute." A refusal written for an operator names jobs and tables, which
is the 11 Sep 2026 defect this sleeve's `no-internals` test exists to catch. The write helper
returns a status and not a string, so the page has nothing internal to leak; a failed run says the
two facts that are the reader's — nothing changed, and they can start another — and not the reason.

**Which table a run is read from is leaf 4's, not this one's.** The UI reads the last run off the
day's payload (`last_scan`, or `last_scan_id` resolved through `GET /twt/scan/{run_id}`), so either
shape leaf 4 ships works without a change here.

**Reversal.** `git revert` the leaf-5 commit; the tree returns to having no action, and TW8.7's
wording is true again unchanged.

---

## TW12.5 — The Chartink scorer is a neighbour of the goldens harness, not a sixth module of it · ⚠ UNREVIEWED

**Context.** `PLAN-SCAN-SYNC.md` leaf 6 added `tools/twt/twt_chartink_gap.py`, and
`test_twt_goldens.py::TestTheHarnessIsWhereItSaysItIs::test_the_five_modules_are_on_disk` went
red — the same red TW7.1 saw when `sweep.py` and `backtest.py` moved in. Two answers were open:
grow `HARNESS_MODULES` to six, or move the scorer out of `tools/twt/` so the harness's five stay
alone in the directory.

**Taken.** Neither, and TW7.1 is why: the directory is the sleeve's tool directory and the test
names *which* file is which. `HARNESS_MODULES` still holds TW2's five; `twt_chartink_gap.py` joins
`OPERATOR_TOOLS` with a reason of its own. The harness is defined by what it *does* — it
reproduces the study's 164 trades against `fixtures/twt/golden_trades.csv` (`gates/twt-2.md`,
`gates/twt-2-harness.md`) — and the scorer grades the **detector against Chartink's export** for
one session. Same directory, same imports, different answer key. Calling it a sixth harness module
would make "the harness" mean "whatever is in the folder", which is the proxy TW7.1 refused.

**Rejected.** (a) `HARNESS_MODULES` → six. It is the one-line fix and it dissolves the only claim
the class exists to make; `test_the_goldens_harness_itself_still_has_exactly_one_writer` is
derived from `HARNESS_MODULES`, so widening it also quietly widens what the writer claim covers.
(b) Moving the file to `tools/twt/analysis/`. The scorer is `twt_scan`, `twt_panel` and
`twt_recall` with a different question asked of them, and it reaches them by a bare
`sys.path.insert(parent)` — a subdirectory buys a longer import for no separation. It would also
rewrite ten CHECK lines in a 16/16 gate, `tools/twt/twt-chartink-pull.sh`'s closing instruction,
`PLAN-SCAN-SYNC.md` §leaf 6 and the module's own usage block: a green gate made stale to keep a
test green is TW7.1's rejected (b) again, and the runbook loses the same way.

**The property that had to survive, and does.** The assertion is still an **exact** set over the
union, so an unnamed tenth file in `tools/twt` still fails it. `_ALLOWED_WRITERS` gained nothing:
the scorer writes no bytes — the box's bars are pulled by `twt-chartink-pull.sh` (a shell file,
not a `*.py`), and the two AST scans over the directory pass unchanged, as does
`_THE_ONE_WRITER`.

**Reversal.** Delete the entry and its comment from `OPERATOR_TOOLS`. Nothing else in the sleeve
reads either name.

"""``baskfy_core.fno`` — the FO run's pure core (``docs/fno/04``; FO1).

Config, the continuous futures series, ATM IV, the calendar from the master and ``trading_day``,
F1's condor, the covered-overnight predicate, sizing, costs, exits and pauses, and the
``RESEARCH.md`` families as functions of frames. Law 1: no database, no network, no disk, no
clock — ``test_fno_purity.py`` asserts it over the source. Law 2: nothing here places an order;
``packages/execution`` is the only path to one.
"""

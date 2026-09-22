"""``baskfy_core.options`` — the options run's pure core (``docs/options/04``; OP1).

Calendar from the NFO master, Black-76 greeks on the parity forward, costs, sizing, the session
machine, the risk ledger, the journal, the shared execution decisions and the backtest engine.
Law 1: no database, no network, no disk, no clock — ``test_options_purity.py`` asserts it over
the source. Law 2: nothing here places an order; ``packages/execution`` is the only path to one.
The sleeves' own signal modules (``condor``, ``directional``, ``expiry_setups``, ``scan``) are
OP4's.
"""

"""HTTP routers — one module per docs/07 section (Prompt 7 deliverable 2).

Metadata, screens and running a screen came with Prompt 7; the instrument factsheet and its
supporting series with Prompt 10; the dashboard, market health and the listings register with
Prompt 11. Portfolios, backtests and billing are later prompts, and a stub route that returns
nothing would be worse than a 404 that says the truth.
"""

from decile_api.routers import instruments, market_data, meta, screens

__all__ = ["instruments", "market_data", "meta", "screens"]

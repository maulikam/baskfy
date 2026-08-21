"""HTTP routers — one module per docs/07 section (Prompt 7 deliverable 2).

Metadata, screens and running a screen came with Prompt 7; the instrument factsheet and its
supporting series with Prompt 10; the dashboard, market health and the listings register with
Prompt 11; auth and the profile with Prompt 12; plans, checkout, the Razorpay webhook and
invoices with Prompt 13. Portfolios and backtests are later prompts, and a stub route that
returns nothing would be worse than a 404 that says the truth.
"""

from decile_api.routers import auth, billing, instruments, market_data, meta, screens

__all__ = ["auth", "billing", "instruments", "market_data", "meta", "screens"]

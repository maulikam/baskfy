# Per-stock regime signal

Maulik, 7 Nov 2026: EMA rule, gates the swing book only. TWT, VBT and Opportunities show the
column and nothing else.

## Rule (`baskfy_core::regime::signal::stock_signal`)

For each stock in a scan's results, plus every index constituent:

1. **Membership.** Every index in `index_constituents` that lists the symbol.
2. **Sector.** The stock's NSE industry (`index_constituents.industry`, from the CSV's
   `Industry` column) maps through `sector_index_map` to related indices. This applies even when
   the stock belongs to no index.
3. **Momentum.** An index is *up* when EMA(10) > EMA(20) on daily closes. This is the swing gate's
   EMA (`baskfy_core::swing::market::gate_ema`, seeded with an SMA, over the last 250 closes).
   It is stored per index per day in `index_stats_daily.ema10 / ema20 / ema_cross`.
4. **GREEN** when any index it reads is up, **RED** when none is. When no index maps at all, or
   none of the mapped indices has an EMA row, the Smallcap-250 rule decides
   (`Nifty Smallcap 250` ema_cross on the day). That row has `fallback = true`.

Rows go into `stock_regime_signal(as_of, symbol)`. `evidence` holds the indices it read, with
`via` member/sector, the two EMAs and `up`, plus `triggered` and `fallback_reason`.

## When it runs

The worker computes signals after each scan writes its rows: swing detect, TWT detect and scan,
VBT detect and rescan, the intraday swing scan, and the 21:30 freshness check. A run never
happens when a page renders. Each run replaces that date's rows.

To run it by hand:

    baskfy-worker regime signals [--date YYYY-MM-DD]   # default: last published date
    baskfy-worker regime ema-backfill                  # EMA columns for all stored history

## Trading (swing only)

`BASKFY_REGIME_MODE=per_stock` is the default; `index_led` and `legacy` still work. In per_stock
mode the market row's gate is the breadth half only (`index_slug = 'per-stock'`), and a new swing
BUY or plan line is allowed only when that symbol's signal for the day is GREEN. A fallback row
or a symbol with no row follows the Smallcap verdict. A refused line becomes a `GATE_RED` skip.
This applies to the evening plan, the pre-market plan, the monitor's trigger plans and the
confirm re-size. Between 09:15 and 15:30 the desk re-reads each index's EMA with today's Kite
level appended, the same way the SW30 live gate does for Smallcap. Where an index has no quote,
its stored value counts.

Per-stock mode switches itself off, and the old gate applies again, when 0062 has not been
applied, when no EMA rows exist, or when no signal rows exist within a week. Exits, plan expiry,
the gateway, GTT stops and caps are unchanged.

## Editing `sector_index_map`

It is a plain table: one row per (industry, index_name). The industry is spelled exactly as in
the NSE CSV's `Industry` column, and the index name exactly as in `index_master.name`.

    INSERT INTO sector_index_map VALUES ('Textiles', 'Nifty India Consumption');
    DELETE FROM sector_index_map WHERE industry = 'Utilities' AND index_name = 'Nifty Energy';

Then re-run `baskfy-worker regime signals` for the date. Textiles, Forest Materials and
Diversified have no mapping in the seed.

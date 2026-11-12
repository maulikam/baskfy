-- STRATEGY-BOOKS (Maulik, 12 Nov 2026): one MY_STRATEGY portfolio per user per strategy, found by key.
ALTER TABLE portfolio
    ADD COLUMN strategy_key text CHECK (strategy_key IN ('twt', 'swing', 'vbt')),
    ADD CONSTRAINT ck_portfolio_strategy_key_is_strategy
        CHECK (strategy_key IS NULL OR source = 'MY_STRATEGY');

CREATE UNIQUE INDEX uq_portfolio_user_strategy_key ON portfolio (user_id, strategy_key)
    WHERE strategy_key IS NOT NULL;

-- Stocks to show / invest on a screen (Maulik, 12 Nov 2026). NULL: every ranked name, invest default 20.
ALTER TABLE screen
    ADD COLUMN invest_count smallint CHECK (invest_count BETWEEN 1 AND 100);

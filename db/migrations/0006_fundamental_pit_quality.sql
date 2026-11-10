-- PM1.13: how far a fundamentals row can be trusted as of its date. NSE filings are as filed; Screener's are today's numbers on a lagged date.
ALTER TABLE fundamental_pit
    ADD COLUMN pit_quality text NOT NULL DEFAULT 'as_filed' CHECK (pit_quality IN ('as_filed', 'lagged')),
    ADD COLUMN restated boolean NOT NULL DEFAULT false;

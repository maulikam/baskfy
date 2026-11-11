-- PM11.2: the doubling model scores every equity too (Maulik, 11 Nov 2026). scope as rr_score_daily's;
-- the paper record reads only 'universe' rows.
ALTER TABLE mb_score_daily
    ADD COLUMN scope text NOT NULL DEFAULT 'universe'
        CHECK (scope IN ('universe', 'quarantine', 'young', 'sme', 'cheap', 'thin'));

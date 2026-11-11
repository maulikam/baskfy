-- PM11.1: the swing model scores every equity, not only its training universe (Maulik, 11 Nov 2026).
-- scope says why a row is outside the universe; picks still come only from 'universe' rows.
ALTER TABLE rr_score_daily
    ADD COLUMN scope text NOT NULL DEFAULT 'universe'
        CHECK (scope IN ('universe', 'quarantine', 'young', 'sme', 'cheap', 'thin'));

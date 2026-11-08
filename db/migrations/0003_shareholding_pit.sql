-- PM2: quarterly shareholding as the market saw it. A feature joins on available_on, never period_end.
CREATE TABLE shareholding_pit (
    instrument_id       bigint NOT NULL REFERENCES instrument(id),
    period_end          date NOT NULL,
    filed_on            date NOT NULL,
    filed_at            timestamp NOT NULL,          -- IST, as NSE stamps it
    available_on        date NOT NULL,               -- first session close that can know it
    is_revision         boolean NOT NULL,
    promoter_pct        numeric(7,4),
    promoter_pledged_pct numeric(7,4),               -- share of the promoters' holding, any encumbrance
    fii_pct             numeric(7,4),
    dii_pct             numeric(7,4),
    mf_pct              numeric(7,4),
    public_pct          numeric(7,4),
    n_shareholders      bigint,
    source              text NOT NULL,
    source_url          text NOT NULL,
    PRIMARY KEY (instrument_id, period_end, filed_on)
);
CREATE UNIQUE INDEX uq_shareholding_pit_source_url ON shareholding_pit (source_url);
CREATE INDEX ix_shareholding_pit_available ON shareholding_pit (instrument_id, available_on);

-- PM1: quarterly results as the market saw them. A feature joins on available_on, never period_end.
CREATE TABLE fundamental_pit (
    instrument_id      bigint NOT NULL REFERENCES instrument(id),
    period_end         date NOT NULL,
    filed_on           date NOT NULL,
    period_type        text NOT NULL CHECK (period_type IN ('consolidated', 'standalone')),
    filed_at           timestamp NOT NULL,          -- IST, as NSE stamps it
    available_on       date NOT NULL,               -- first session close that can know it
    period_start       date NOT NULL,
    is_revision        boolean NOT NULL,
    revenue            numeric(20,2),
    other_income       numeric(20,2),
    finance_costs      numeric(20,2),
    depreciation       numeric(20,2),
    ebitda             numeric(20,2),
    pbt                numeric(20,2),
    pat                numeric(20,2),
    eps_diluted        numeric(14,4),
    shares_outstanding bigint,
    total_debt         numeric(20,2),               -- period end; half-yearly, from about Sep 2022
    total_equity       numeric(20,2),               -- period end; half-yearly, from about Sep 2022
    cfo                numeric(20,2),               -- year to date; half-yearly, from about Sep 2021
    capex              numeric(20,2),               -- year to date; half-yearly, from about Sep 2021
    source             text NOT NULL,
    source_url         text NOT NULL,
    PRIMARY KEY (instrument_id, period_end, filed_on, period_type)
);
CREATE UNIQUE INDEX uq_fundamental_pit_source_url ON fundamental_pit (source_url);
CREATE INDEX ix_fundamental_pit_available ON fundamental_pit (instrument_id, available_on);

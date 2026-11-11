-- PM8: the MB paper book. Paper only: nothing here is an order, a plan line or a broker call.
-- source 'backtest' rows are the seeded walk-forward history; 'live' rows start with the first tranche after deploy.
CREATE TABLE mb_paper_tranche (
    id          serial PRIMARY KEY,
    source      text NOT NULL CHECK (source IN ('backtest', 'live')),
    signal_date date NOT NULL,              -- the month's first session; entries at the next session's open
    model_id    integer REFERENCES mb_model(id),
    watch       jsonb NOT NULL,             -- the top 50 investable [instrument_id, rank] the trigger lines watch
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, signal_date)
);

CREATE TABLE mb_paper_line (
    tranche_id    integer NOT NULL REFERENCES mb_paper_tranche(id),
    line          text NOT NULL CHECK (line IN ('book', 'hi63')),   -- hi63 lines carry both shadow sizes
    instrument_id bigint NOT NULL REFERENCES instrument(id),
    rank          integer NOT NULL,
    signal_date   date NOT NULL,            -- book: the tranche's; hi63: the session the 63-day high fired
    -- refreshed every night from the corrected bars:
    entry_date    date,
    entry_px      numeric(14,4),
    last_date     date,
    mult          numeric(12,6),            -- last close / entry
    max_mult      numeric(12,6),            -- highest high / entry so far
    sessions_held integer,
    closed        boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tranche_id, line, instrument_id)
);

CREATE TABLE mb_paper_mark (
    date     date NOT NULL,
    source   text NOT NULL CHECK (source IN ('backtest', 'live')),
    book     text NOT NULL CHECK (book IN ('book', 'hi63_20', 'hi63_16', 'book_overlay')),
    equity   numeric(14,6) NOT NULL,
    invested numeric(8,6) NOT NULL,
    PRIMARY KEY (date, source, book)
);

CREATE TABLE mb_paper_state (key text PRIMARY KEY, value text NOT NULL);

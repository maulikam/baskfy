-- PM11: the swing-picks model (XGBoost, trail −7 % / 12 % for up to 60 sessions) and its equal-weight NIFTY 500 switch.
-- Read-only sleeve: picks are paper; nothing here is an order, a plan line or a broker call.
CREATE TABLE rr_model (
    id           serial PRIMARY KEY,
    trained_on   date NOT NULL,             -- last signal session with a completed label
    label        text NOT NULL,             -- 'trail_7_12_h60_gain_14'
    features     jsonb NOT NULL,
    params       jsonb NOT NULL,
    path         text NOT NULL,
    rows         integer NOT NULL,
    positives    integer NOT NULL,
    published_at timestamptz,
    published_by text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rr_score_daily (
    date          date NOT NULL,
    instrument_id bigint NOT NULL REFERENCES instrument(id),
    model_id      integer NOT NULL REFERENCES rr_model(id),
    score         numeric(10,6) NOT NULL,
    rank          integer NOT NULL,
    universe_n    integer NOT NULL,
    investable    boolean NOT NULL,
    close_raw     numeric(14,2),
    PRIMARY KEY (date, instrument_id)
);
CREATE INDEX rr_score_daily_instrument ON rr_score_daily (instrument_id, date);

-- NIFTY500 Equal Weight at the session's close: picks only when above its 200-session average AND 10 over 20.
CREATE TABLE rr_gate_daily (
    date      date PRIMARY KEY,
    ew_close  numeric(18,4) NOT NULL,
    ma10      numeric(18,4),
    ma20      numeric(18,4),
    ma200     numeric(18,4),
    above_200 boolean NOT NULL,
    up_10_20  boolean NOT NULL,
    is_on     boolean NOT NULL
);

-- One paper trade per pick, refreshed nightly from the corrected bars (prices in today's adjusted terms).
CREATE TABLE rr_pick (
    signal_date   date NOT NULL,
    instrument_id bigint NOT NULL REFERENCES instrument(id),
    model_id      integer NOT NULL REFERENCES rr_model(id),
    pick_rank     smallint NOT NULL,
    score         numeric(10,6) NOT NULL,
    status        text NOT NULL CHECK (status IN ('waiting', 'open', 'closed')),
    entry_date    date,
    entry_px      numeric(14,2),
    stop_px       numeric(14,2),           -- the level in force for the next session
    peak_px       numeric(14,2),
    last_date     date,
    last_px       numeric(14,2),
    ret           numeric(10,4),           -- last (or exit) / entry − 1, before costs
    sessions_held integer,
    exit_date     date,
    exit_px       numeric(14,2),
    exit_reason   text CHECK (exit_reason IN ('stop', 'trail', 'time')),
    PRIMARY KEY (signal_date, instrument_id)
);

-- The walk-forward result the model was adopted on (PM10f, ew_10_20_200), 'all' plus one row a year.
CREATE TABLE rr_backtest (
    period        text PRIMARY KEY,
    trades        integer NOT NULL,
    net           numeric(10,4) NOT NULL,   -- mean per trade after 0.5 % costs
    win_rate      numeric(6,4),
    profit_factor numeric(8,4),
    worst         numeric(10,4),
    on_share      numeric(6,4)
);

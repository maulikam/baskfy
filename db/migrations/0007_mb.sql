-- PM6: the MB sleeve's saved models and nightly scores. Read-only sleeve: nothing here feeds an order.
CREATE TABLE mb_model (
    id            serial PRIMARY KEY,
    trained_on    date NOT NULL,            -- last session with a completed label in the training set
    label         text NOT NULL,            -- 'touch_2x_250'
    features      jsonb NOT NULL,
    params        jsonb NOT NULL,
    path          text NOT NULL,
    rows          integer NOT NULL,
    positives     integer NOT NULL,
    spearman_prev numeric(6,4),             -- vs the model it replaces, same day; null for the first
    published_at  timestamptz,              -- null = trained, not scoring (PM6.3 ack rule)
    published_by  text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE mb_score_daily (
    date              date NOT NULL,
    instrument_id     bigint NOT NULL REFERENCES instrument(id),
    model_id          integer NOT NULL REFERENCES mb_model(id),
    score             numeric(10,6) NOT NULL,
    rank              integer NOT NULL,
    universe_n        integer NOT NULL,
    investable        boolean NOT NULL,
    hi63_trigger      boolean NOT NULL,
    hi63_trigger_date date,
    PRIMARY KEY (date, instrument_id)
);
CREATE INDEX mb_score_daily_instrument ON mb_score_daily (instrument_id, date);

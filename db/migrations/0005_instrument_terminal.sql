-- PM0.7: how a name stopped trading. A name still trading has no row. Bars for resurrected names
-- come from the bhavcopy archive; see DECISIONS-PM PM0.7.
CREATE TABLE instrument_terminal (
    instrument_id bigint PRIMARY KEY REFERENCES instrument(id),
    status        text NOT NULL CHECK (status IN ('merged', 'delisted', 'suspended')),
    reason        text,
    last_bar      date NOT NULL,
    source        text NOT NULL
);

-- PM3: NSE's classification, one row per instrument, refreshed nightly from the quote.
CREATE TABLE instrument_sector (
    instrument_id  bigint PRIMARY KEY REFERENCES instrument(id),
    macro          text,
    sector         text,
    industry       text,
    basic_industry text,
    as_of          date NOT NULL
);
COMMENT ON TABLE instrument_sector IS 'Today''s classification. Treating it as static across history is a known small look-ahead leak (DECISIONS-PM PM3.1); every research feature built from it inherits that.';

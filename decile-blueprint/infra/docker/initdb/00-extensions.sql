-- Extensions required by docs/04-data-model.md.
--   timescaledb : hypertables for ohlcv_daily, factor_daily, index_member_daily
--   citext      : app_user.email
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS citext;

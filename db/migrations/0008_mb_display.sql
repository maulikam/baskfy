-- PM7: display-only columns beside each score, computed by the scorer as research computes them. Never a model input.
ALTER TABLE mb_score_daily
    ADD COLUMN close_raw       numeric(14,2),
    ADD COLUMN turnover20      numeric(18,2),   -- rupees, 20-session mean of close_raw × volume_raw
    ADD COLUMN r252            numeric(10,4),   -- 252-session return on the adjusted close
    ADD COLUMN dist_ath        numeric(10,4),   -- close / all-time adjusted high − 1
    ADD COLUMN sessions_listed integer,
    ADD COLUMN ttm_rev_yoy     numeric(10,4),   -- fundamental_pit, available_on ≤ date (PM1)
    ADD COLUMN ttm_pat_yoy     numeric(10,4);

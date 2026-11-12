-- GTT-TRAIL (Maulik, 12 Nov 2026): per-strategy trailing-stop policy and its journal. No row = off.
CREATE TABLE public.gtt_trail_policy (
    strategy text PRIMARY KEY CHECK (strategy IN ('swing', 'twt', 'vbt', 'screen', 'weekly')),
    enabled boolean NOT NULL DEFAULT false,
    breakeven_after_pct numeric(6, 2) NOT NULL CHECK (breakeven_after_pct BETWEEN 0.5 AND 100),
    breakeven_buffer_pct numeric(6, 2) NOT NULL CHECK (breakeven_buffer_pct BETWEEN 0 AND 5),
    trail_after_pct numeric(6, 2) NOT NULL CHECK (trail_after_pct BETWEEN 0.5 AND 200),
    trail_pct numeric(6, 2) NOT NULL CHECK (trail_pct BETWEEN 1 AND 50),
    min_step_pct numeric(6, 2) NOT NULL CHECK (min_step_pct BETWEEN 0 AND 10),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text
);

CREATE TABLE public.gtt_trail_journal (
    id bigserial PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT now(),
    source text NOT NULL CHECK (source IN ('daily', 'manual')),
    strategy text NOT NULL,
    position_id bigint NOT NULL,
    symbol text NOT NULL,
    qty integer NOT NULL,
    entry numeric(14, 2),
    as_of date,
    highest_close numeric(14, 2),
    last_close numeric(14, 2),
    old_stop numeric(14, 2),
    new_stop numeric(14, 2),
    reason text NOT NULL,
    outcome text NOT NULL,
    detail text NOT NULL DEFAULT ''
);

CREATE INDEX ix_gtt_trail_journal_at ON public.gtt_trail_journal (at DESC);

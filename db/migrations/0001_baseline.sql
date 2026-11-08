-- Baseline: Alembic head 0063_mtf_leverage, frozen 8 Nov 2026. Later changes are new numbered files.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS citext;

COMMENT ON SCHEMA public IS 'standard public schema';

CREATE TYPE public.fo_sleeve AS ENUM (
    'F1N',
    'F1B',
    'F2',
    'F3N',
    'F3B'
);

CREATE TYPE public.op_sleeve AS ENUM (
    'O1M',
    'O1W',
    'O2',
    'O3A',
    'O3B'
);

CREATE FUNCTION public.portfolio_holding_attribute_broker_account() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
    AS $$
DECLARE
    v_user_id  bigint;
    v_account  bigint;
BEGIN
    IF NEW.broker_account_id IS NOT NULL THEN
        RETURN NEW;
    END IF;

    SELECT p.user_id, p.broker_account_id
      INTO v_user_id, v_account
      FROM public.portfolio p
     WHERE p.id = NEW.portfolio_id;

    IF v_account IS NULL THEN
        SELECT a.id
          INTO v_account
          FROM public.broker_account a
         WHERE a.user_id = v_user_id
         ORDER BY (a.broker_id <> 'zerodha'), a.id
         LIMIT 1;
    END IF;

    IF v_account IS NULL THEN
        INSERT INTO public.broker_account (user_id, broker_id, label)
        VALUES (v_user_id, 'zerodha', 'primary')
        RETURNING id INTO v_account;
    END IF;

    NEW.broker_account_id := v_account;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.portfolio_holding_attribute_kind() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
    AS $$
BEGIN
    -- A writer that named the kind is left alone: it may be deliberately inserting into a
    -- monitoring view, and second-guessing it here would silently move the row.
    IF NEW.portfolio_kind IS NOT NULL THEN
        RETURN NEW;
    END IF;

    SELECT p.kind
      INTO NEW.portfolio_kind
      FROM public.portfolio p
     WHERE p.id = NEW.portfolio_id;

    -- A NULL here means the portfolio does not exist. Returning the row unchanged lets the
    -- foreign key say so, which is a better error than anything this function could raise.
    RETURN NEW;
END;
$$;

CREATE TABLE public.market_health_daily (
    index_id smallint NOT NULL,
    date date NOT NULL,
    pct_above_200dma numeric(7,4),
    pct_above_50dma numeric(7,4),
    pct_within_10pct_ath numeric(7,4),
    pct_ret_1y_positive numeric(7,4),
    constituent_count integer,
    pct_above_20dma numeric(7,4)
);

CREATE TABLE public.index_snapshot_daily (
    index_id smallint NOT NULL,
    date date NOT NULL,
    level numeric(18,4),
    change_abs numeric(18,4),
    change_pct numeric(10,4),
    pe numeric(14,4),
    pb numeric(14,4),
    div_yield numeric(10,4)
);

CREATE TABLE public.account_deletion (
    user_id bigint NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    purge_after timestamp with time zone NOT NULL,
    cancelled_at timestamp with time zone,
    purged_at timestamp with time zone
);

CREATE TABLE public.admin_action (
    id bigint NOT NULL,
    actor_user_id bigint NOT NULL,
    action character varying NOT NULL,
    target character varying NOT NULL,
    detail jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.admin_action ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.admin_action_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.api_key (
    id bigint NOT NULL,
    public_id character varying NOT NULL,
    user_id bigint NOT NULL,
    name character varying NOT NULL,
    prefix character varying NOT NULL,
    token_hash character varying NOT NULL,
    scopes jsonb DEFAULT '[]'::jsonb NOT NULL,
    rate_limit_per_minute integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone,
    revoked_reason character varying,
    rotated_from_id bigint
);

ALTER TABLE public.api_key ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.api_key_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.api_key_usage_daily (
    api_key_id bigint NOT NULL,
    date date NOT NULL,
    requests bigint DEFAULT 0 NOT NULL,
    throttled bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.app_user (
    id bigint NOT NULL,
    public_id character varying NOT NULL,
    email public.citext NOT NULL,
    password_hash character varying,
    name character varying,
    email_verified_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    is_staff boolean DEFAULT false NOT NULL,
    session_epoch integer DEFAULT 0 NOT NULL
);

ALTER TABLE public.app_user ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.app_user_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.auth_identity (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    provider character varying NOT NULL,
    subject character varying NOT NULL,
    email_at_provider character varying,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_login_at timestamp with time zone
);

CREATE SEQUENCE public.auth_identity_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.auth_identity_id_seq OWNED BY public.auth_identity.id;

CREATE TABLE public.auth_lockout (
    identifier public.citext NOT NULL,
    failures integer NOT NULL,
    first_failure_at timestamp with time zone,
    last_failure_at timestamp with time zone,
    locked_until timestamp with time zone,
    notified_at timestamp with time zone
);

CREATE TABLE public.auth_token (
    id bigint NOT NULL,
    user_id bigint,
    email public.citext NOT NULL,
    purpose character varying NOT NULL,
    token_hash character varying NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    attempts integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_auth_token_auth_token_purpose CHECK (((purpose)::text = ANY ((ARRAY['otp'::character varying, 'email_verify'::character varying, 'password_reset'::character varying])::text[])))
);

ALTER TABLE public.auth_token ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.auth_token_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.auth_verification_token (
    identifier character varying NOT NULL,
    token character varying NOT NULL,
    expires timestamp with time zone NOT NULL
);

CREATE TABLE public.backtest (
    id bigint NOT NULL,
    public_id character varying NOT NULL,
    user_id bigint NOT NULL,
    screen_id bigint,
    config jsonb NOT NULL,
    status character varying NOT NULL,
    metrics jsonb,
    equity_curve jsonb,
    trades_key character varying,
    error character varying,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    started_at timestamp with time zone,
    CONSTRAINT ck_backtest_backtest_status CHECK (((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying, 'done'::character varying, 'failed'::character varying])::text[])))
);

ALTER TABLE public.backtest ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.backtest_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.basket_snapshot (
    id bigint NOT NULL,
    as_of date NOT NULL,
    screen_run_id character varying NOT NULL,
    data_version integer NOT NULL,
    payload jsonb NOT NULL,
    computed_ms integer DEFAULT 0 NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.basket_snapshot_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.basket_snapshot_id_seq OWNED BY public.basket_snapshot.id;

CREATE TABLE public.broker_account (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    broker_id character varying NOT NULL,
    label character varying DEFAULT 'primary'::character varying NOT NULL,
    kite_user_id character varying,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    holdings_synced_at timestamp with time zone
);

ALTER TABLE public.broker_account ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.broker_account_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.broker_cash (
    broker_account_id bigint NOT NULL,
    balance numeric(20,2) DEFAULT '0'::numeric NOT NULL,
    as_of date NOT NULL
);

CREATE TABLE public.broker_trade (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    broker_account_id bigint NOT NULL,
    instrument_id bigint,
    symbol character varying NOT NULL,
    isin character varying,
    exchange character varying NOT NULL,
    side character varying NOT NULL,
    quantity numeric(20,4) NOT NULL,
    price numeric(18,4) NOT NULL,
    trade_date date NOT NULL,
    executed_at timestamp with time zone,
    trade_id character varying NOT NULL,
    order_id character varying,
    source character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_broker_trade_broker_trade_price_non_negative CHECK ((price >= (0)::numeric)),
    CONSTRAINT ck_broker_trade_broker_trade_quantity_positive CHECK ((quantity > (0)::numeric)),
    CONSTRAINT ck_broker_trade_broker_trade_side CHECK (((side)::text = ANY ((ARRAY['BUY'::character varying, 'SELL'::character varying])::text[]))),
    CONSTRAINT ck_broker_trade_broker_trade_source CHECK (((source)::text = ANY ((ARRAY['CONSOLE_CSV'::character varying, 'KITE_API'::character varying])::text[])))
);

ALTER TABLE public.broker_trade ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.broker_trade_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.candidate_review_label (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    review_key text NOT NULL,
    state jsonb NOT NULL,
    instrument_id bigint NOT NULL,
    symbol text NOT NULL,
    label character varying NOT NULL,
    note text,
    laya_label character varying,
    laya_confidence numeric(6,4),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_candidate_review_label_label_known CHECK (((label)::text = ANY ((ARRAY['look_first'::character varying, 'worth_a_look'::character varying, 'skip'::character varying])::text[])))
);

ALTER TABLE public.candidate_review_label ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.candidate_review_label_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.catalyst_tag_correction (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    headline_key text NOT NULL,
    headline text NOT NULL,
    event_type character varying NOT NULL,
    note text,
    rules_event_type character varying,
    laya_event_type character varying,
    laya_confidence numeric(6,4),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_catalyst_tag_correction_event_type_known CHECK (((event_type)::text = ANY ((ARRAY['earnings'::character varying, 'order'::character varying, 'approval'::character varying, 'fundraising'::character varying, 'governance'::character varying, 'corporate_action'::character varying, 'routine'::character varying, 'other'::character varying])::text[])))
);

ALTER TABLE public.catalyst_tag_correction ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.catalyst_tag_correction_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_basket (
    id bigint NOT NULL,
    slug character varying NOT NULL,
    name character varying NOT NULL,
    manager_id bigint NOT NULL,
    type character varying NOT NULL,
    access character varying NOT NULL,
    visibility character varying NOT NULL,
    categories character varying[] DEFAULT '{}'::character varying[] NOT NULL,
    description_md text,
    rationale_md text,
    rebalance_frequency character varying NOT NULL,
    benchmark_instrument_id bigint,
    launched_at date,
    next_review_at date,
    source character varying NOT NULL,
    scan_strategy_key character varying,
    archived_at timestamp with time zone,
    source_screen_id bigint,
    CONSTRAINT ck_cb_basket_cb_basket_access CHECK (((access)::text = ANY ((ARRAY['FREE'::character varying, 'FEE'::character varying])::text[]))),
    CONSTRAINT ck_cb_basket_cb_basket_rebalance_frequency CHECK (((rebalance_frequency)::text = ANY ((ARRAY['WEEKLY'::character varying, 'MONTHLY'::character varying, 'QUARTERLY'::character varying, 'ANNUAL'::character varying, 'NEED_BASIS'::character varying])::text[]))),
    CONSTRAINT ck_cb_basket_cb_basket_source CHECK (((source)::text = ANY ((ARRAY['SCAN'::character varying, 'MANUAL'::character varying, 'SCREEN'::character varying])::text[]))),
    CONSTRAINT ck_cb_basket_cb_basket_type CHECK (((type)::text = ANY ((ARRAY['STOCK'::character varying, 'MF'::character varying, 'US'::character varying])::text[]))),
    CONSTRAINT ck_cb_basket_cb_basket_visibility CHECK (((visibility)::text = ANY ((ARRAY['PUBLISHED'::character varying, 'PRIVATE'::character varying])::text[])))
);

ALTER TABLE public.cb_basket ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_basket_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_basket_version (
    id bigint NOT NULL,
    basket_id bigint NOT NULL,
    version_no integer NOT NULL,
    effective_date date NOT NULL,
    label character varying NOT NULL,
    added_count integer DEFAULT 0 NOT NULL,
    removed_count integer DEFAULT 0 NOT NULL,
    notes_md text,
    source_scan_run_id character varying,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_cb_basket_version_cb_basket_version_label CHECK (((label)::text = ANY ((ARRAY['CHANGED'::character varying, 'NO_CHANGE'::character varying, 'GENESIS'::character varying])::text[])))
);

ALTER TABLE public.cb_basket_version ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_basket_version_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_collection (
    id bigint NOT NULL,
    slug character varying NOT NULL,
    title character varying NOT NULL,
    subtitle character varying,
    basket_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    "position" smallint DEFAULT '0'::smallint NOT NULL,
    curated_meta jsonb
);

ALTER TABLE public.cb_collection ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_collection_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_constituent (
    id bigint NOT NULL,
    version_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    segment character varying NOT NULL,
    weight numeric(7,4) NOT NULL
);

ALTER TABLE public.cb_constituent ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_constituent_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_dividend (
    id bigint NOT NULL,
    investment_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    ex_date date NOT NULL,
    amount_per_share numeric(18,2) NOT NULL,
    qty_held numeric(20,4) NOT NULL,
    total numeric(20,2) NOT NULL,
    source character varying NOT NULL,
    CONSTRAINT ck_cb_dividend_cb_dividend_source CHECK (((source)::text = 'CORPORATE_ACTIONS'::text))
);

ALTER TABLE public.cb_dividend ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_dividend_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_fee_ledger (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    batch_id bigint NOT NULL,
    kind character varying NOT NULL,
    base_fee numeric(12,2) NOT NULL,
    gst numeric(12,2) NOT NULL,
    total numeric(12,2) NOT NULL,
    collected boolean DEFAULT false NOT NULL,
    accrued_at timestamp with time zone NOT NULL
);

ALTER TABLE public.cb_fee_ledger ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_fee_ledger_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_investment (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    basket_id bigint NOT NULL,
    status character varying NOT NULL,
    version_applied_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    exited_at timestamp with time zone,
    last_invested_at timestamp with time zone,
    broker_account_id bigint NOT NULL,
    portfolio_id bigint,
    CONSTRAINT ck_cb_investment_cb_investment_status CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'EXITED'::character varying])::text[])))
);

CREATE TABLE public.cb_investment_holding (
    id bigint NOT NULL,
    investment_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    qty numeric(20,4) NOT NULL,
    avg_price numeric(18,2) NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.cb_investment_holding ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_investment_holding_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

ALTER TABLE public.cb_investment ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_investment_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_manager (
    id bigint NOT NULL,
    slug character varying NOT NULL,
    name character varying NOT NULL,
    kind character varying NOT NULL,
    sebi_reg_no character varying,
    bio text,
    strategies character varying[] DEFAULT '{}'::character varying[] NOT NULL,
    disclosures_md text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id bigint,
    state character varying DEFAULT 'DRAFT'::character varying NOT NULL,
    sebi_reg_type character varying DEFAULT 'NONE'::character varying NOT NULL,
    sebi_reg_valid_from date,
    sebi_reg_valid_to date,
    sebi_reg_verified_at timestamp with time zone,
    CONSTRAINT ck_cb_manager_cb_manager_kind CHECK (((kind)::text = ANY ((ARRAY['ENGINE'::character varying, 'HUMAN'::character varying, 'EXTERNAL'::character varying])::text[]))),
    CONSTRAINT ck_cb_manager_cb_manager_sebi_reg_pairing CHECK (((((sebi_reg_type)::text = 'NONE'::text) AND (sebi_reg_no IS NULL)) OR (((sebi_reg_type)::text <> 'NONE'::text) AND (sebi_reg_no IS NOT NULL)))),
    CONSTRAINT ck_cb_manager_cb_manager_sebi_reg_type CHECK (((sebi_reg_type)::text = ANY ((ARRAY['NONE'::character varying, 'RESEARCH_ANALYST'::character varying, 'INVESTMENT_ADVISER'::character varying, 'PORTFOLIO_MANAGER'::character varying, 'AIF'::character varying, 'MF_DISTRIBUTOR'::character varying, 'UNKNOWN'::character varying])::text[]))),
    CONSTRAINT ck_cb_manager_cb_manager_sebi_reg_window CHECK (((sebi_reg_valid_to IS NULL) OR (sebi_reg_valid_from IS NULL) OR (sebi_reg_valid_to >= sebi_reg_valid_from))),
    CONSTRAINT ck_cb_manager_cb_manager_state CHECK (((state)::text = ANY ((ARRAY['DRAFT'::character varying, 'SUBMITTED'::character varying, 'APPROVED'::character varying, 'REJECTED'::character varying, 'SUSPENDED'::character varying])::text[])))
);

ALTER TABLE public.cb_manager ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_manager_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_manager_revenue_share (
    id bigint NOT NULL,
    manager_id bigint NOT NULL,
    rate_bps integer NOT NULL,
    effective_from date NOT NULL,
    effective_to date,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_cb_manager_revenue_share_cb_manager_revenue_share_rate CHECK (((rate_bps >= 0) AND (rate_bps <= 10000))),
    CONSTRAINT ck_cb_manager_revenue_share_cb_manager_revenue_share_window CHECK (((effective_to IS NULL) OR (effective_to > effective_from)))
);

CREATE SEQUENCE public.cb_manager_revenue_share_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.cb_manager_revenue_share_id_seq OWNED BY public.cb_manager_revenue_share.id;

CREATE TABLE public.cb_metrics (
    basket_id bigint NOT NULL,
    as_of_date date NOT NULL,
    min_amount numeric(20,2),
    volatility_bucket character varying,
    volatility_value numeric(18,10),
    ret_1m numeric(14,2),
    ret_6m numeric(14,2),
    ret_1y numeric(14,2),
    cagr_3y numeric(14,2),
    cagr_5y numeric(14,2),
    since_inception_pct numeric(14,2),
    computed_at timestamp with time zone NOT NULL,
    volatility_basis text,
    months_available integer,
    return_convention text DEFAULT 'PRICE_RETURN'::text NOT NULL,
    dividends_included boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_cb_metrics_cb_metrics_months_available CHECK (((months_available IS NULL) OR (months_available >= 0))),
    CONSTRAINT ck_cb_metrics_cb_metrics_return_convention CHECK ((return_convention = ANY (ARRAY['PRICE_RETURN'::text, 'TOTAL_RETURN'::text]))),
    CONSTRAINT ck_cb_metrics_cb_metrics_volatility_basis CHECK (((volatility_basis IS NULL) OR (volatility_basis = ANY (ARRAY['BASKET_252D'::text, 'BASKET_FULL_HISTORY'::text, 'CONSTITUENT_WEIGHTED'::text])))),
    CONSTRAINT ck_cb_metrics_cb_metrics_volatility_bucket CHECK (((volatility_bucket IS NULL) OR ((volatility_bucket)::text = ANY ((ARRAY['LOW'::character varying, 'MED'::character varying, 'HIGH'::character varying])::text[]))))
);

CREATE TABLE public.cb_order_batch (
    id bigint NOT NULL,
    investment_id bigint NOT NULL,
    kind character varying NOT NULL,
    requested_amount numeric(20,2),
    desk_plan_id character varying,
    status character varying NOT NULL,
    fee_entry_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    executed_at timestamp with time zone,
    user_id bigint NOT NULL,
    broker_account_id bigint NOT NULL,
    CONSTRAINT ck_cb_order_batch_cb_order_batch_kind CHECK (((kind)::text = ANY ((ARRAY['BUY'::character varying, 'INVEST_MORE'::character varying, 'SIP'::character varying, 'REBALANCE'::character varying, 'EXIT'::character varying, 'PARTIAL_EXIT'::character varying, 'CUSTOMIZE'::character varying])::text[]))),
    CONSTRAINT ck_cb_order_batch_cb_order_batch_status CHECK (((status)::text = ANY ((ARRAY['DRAFT'::character varying, 'PLANNED'::character varying, 'EXPIRED'::character varying, 'EXECUTED'::character varying, 'PARTIAL'::character varying, 'CANCELLED'::character varying])::text[])))
);

ALTER TABLE public.cb_order_batch ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_order_batch_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_pending_action (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    type character varying NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    dismissed_at timestamp with time zone,
    resolved_at timestamp with time zone,
    CONSTRAINT ck_cb_pending_action_cb_pending_action_type CHECK (((type)::text = ANY ((ARRAY['DRIFT'::character varying, 'REBALANCE_AVAILABLE'::character varying, 'SIP_DUE'::character varying, 'GENERIC'::character varying])::text[])))
);

ALTER TABLE public.cb_pending_action ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_pending_action_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_plan (
    id bigint NOT NULL,
    basket_id bigint,
    manager_id bigint,
    duration character varying NOT NULL,
    price numeric(12,2) NOT NULL,
    CONSTRAINT ck_cb_plan_cb_plan_duration CHECK (((duration)::text = ANY ((ARRAY['M1'::character varying, 'M3'::character varying, 'M6'::character varying, 'Y1'::character varying])::text[])))
);

ALTER TABLE public.cb_plan ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_plan_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_sip_plan (
    id bigint NOT NULL,
    investment_id bigint NOT NULL,
    amount numeric(20,2) NOT NULL,
    day_of_month smallint NOT NULL,
    mode character varying NOT NULL,
    status character varying NOT NULL,
    next_fire_date date NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_cb_sip_plan_cb_sip_plan_mode CHECK (((mode)::text = 'REMINDER'::text)),
    CONSTRAINT ck_cb_sip_plan_cb_sip_plan_status CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'PAUSED'::character varying])::text[])))
);

ALTER TABLE public.cb_sip_plan ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_sip_plan_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_subscription (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    plan_id bigint NOT NULL,
    start_at timestamp with time zone NOT NULL,
    renew_at timestamp with time zone,
    status character varying NOT NULL,
    auto_renew boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_cb_subscription_cb_subscription_status CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'CANCELLED'::character varying, 'LAPSED'::character varying])::text[])))
);

ALTER TABLE public.cb_subscription ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_subscription_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_update_post (
    id bigint NOT NULL,
    basket_id bigint,
    manager_id bigint,
    title character varying NOT NULL,
    body_md text NOT NULL,
    published_at timestamp with time zone NOT NULL,
    source character varying NOT NULL,
    CONSTRAINT ck_cb_update_post_cb_update_post_source CHECK (((source)::text = ANY ((ARRAY['ENGINE'::character varying, 'HUMAN'::character varying])::text[])))
);

ALTER TABLE public.cb_update_post ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_update_post_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.cb_user_rebalance_state (
    user_id bigint NOT NULL,
    version_id bigint NOT NULL,
    state character varying NOT NULL,
    decided_at timestamp with time zone,
    CONSTRAINT ck_cb_user_rebalance_state_cb_user_rebalance_state_state CHECK (((state)::text = ANY ((ARRAY['APPLIED'::character varying, 'SKIPPED'::character varying, 'PENDING'::character varying])::text[])))
);

CREATE TABLE public.cb_watchlist_item (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    basket_id bigint NOT NULL,
    watched_at timestamp with time zone NOT NULL,
    nav_at_watch numeric(18,2)
);

ALTER TABLE public.cb_watchlist_item ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.cb_watchlist_item_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.consent_record (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    kind character varying NOT NULL,
    document_version character varying NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    withdrawn_at timestamp with time zone,
    source_ip character varying,
    user_agent character varying,
    CONSTRAINT ck_consent_record_consent_record_kind CHECK (((kind)::text = ANY ((ARRAY['terms'::character varying, 'privacy'::character varying, 'marketing'::character varying])::text[])))
);

ALTER TABLE public.consent_record ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.consent_record_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.corporate_action (
    id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    action_type character varying NOT NULL,
    ex_date date NOT NULL,
    ratio_from numeric(18,6),
    ratio_to numeric(18,6),
    amount numeric(18,4),
    raw jsonb NOT NULL,
    CONSTRAINT ck_corporate_action_corporate_action_type CHECK (((action_type)::text = ANY ((ARRAY['dividend'::character varying, 'split'::character varying, 'bonus'::character varying, 'rights'::character varying, 'demerger'::character varying])::text[])))
);

ALTER TABLE public.corporate_action ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.corporate_action_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.desk_score_daily (
    instrument_id bigint NOT NULL,
    date date NOT NULL,
    score numeric(6,1),
    score_rank integer,
    a_trend numeric(8,4),
    b_momentum numeric(8,4),
    c_sharpe numeric(8,4),
    d_consistency numeric(8,4),
    e_liquidity numeric(8,4),
    f_penalty numeric(8,4),
    ext_over_20dma numeric(10,4),
    reject character varying(200) DEFAULT ''::character varying NOT NULL,
    score_version character varying(32) NOT NULL
);

CREATE TABLE public.entitlement_override (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    feature character varying NOT NULL,
    effect character varying NOT NULL,
    value bigint,
    granted_by_user_id bigint NOT NULL,
    reason character varying NOT NULL,
    expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_entitlement_override_ck_entitlement_override_entitle_056a CHECK (((effect)::text = ANY ((ARRAY['grant'::character varying, 'revoke'::character varying])::text[])))
);

ALTER TABLE public.entitlement_override ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.entitlement_override_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.eq_minute_bar (
    instrument_id bigint NOT NULL,
    ts timestamp with time zone NOT NULL,
    open numeric(18,2) NOT NULL,
    high numeric(18,2) NOT NULL,
    low numeric(18,2) NOT NULL,
    close numeric(18,2) NOT NULL,
    volume bigint DEFAULT '0'::bigint NOT NULL,
    source character varying(16) NOT NULL,
    CONSTRAINT ck_eq_minute_bar_high_not_below_low CHECK ((high >= low)),
    CONSTRAINT ck_eq_minute_bar_source_known CHECK (((source)::text = ANY ((ARRAY['KITE_HIST'::character varying, 'TICKS'::character varying])::text[]))),
    CONSTRAINT ck_eq_minute_bar_volume_non_negative CHECK ((volume >= 0))
);

CREATE TABLE public.exchange (
    id smallint NOT NULL,
    code character varying NOT NULL
);

CREATE TABLE public.factor_daily (
    instrument_id bigint NOT NULL,
    date date NOT NULL,
    close numeric(18,2),
    close_raw numeric(18,2),
    ret_1m numeric(14,2),
    ret_3m numeric(14,2),
    ret_6m numeric(14,2),
    ret_9m numeric(14,2),
    ret_12m numeric(14,2),
    ret_12m_minus_1m numeric(14,2),
    ret_12m_minus_2m numeric(14,2),
    vol_1m numeric(18,10),
    vol_3m numeric(18,10),
    vol_6m numeric(18,10),
    vol_9m numeric(18,10),
    vol_12m numeric(18,10),
    sharpe_1m numeric(14,2),
    sharpe_3m numeric(14,2),
    sharpe_6m numeric(14,2),
    sharpe_9m numeric(14,2),
    sharpe_12m numeric(14,2),
    rsi_1m numeric(10,4),
    rsi_3m numeric(10,4),
    rsi_6m numeric(10,4),
    rsi_9m numeric(10,4),
    rsi_12m numeric(10,4),
    beta_12m numeric(18,10),
    ma_20 numeric(18,2),
    ma_50 numeric(18,2),
    ma_100 numeric(18,2),
    ma_200 numeric(18,2),
    high_1y numeric(18,2),
    high_ath numeric(18,2),
    away_high_1y numeric(10,2),
    away_high_ath numeric(10,2),
    pos_days_1m numeric(7,2),
    pos_days_3m numeric(7,2),
    pos_days_6m numeric(7,2),
    pos_days_9m numeric(7,2),
    pos_days_12m numeric(7,2),
    circuits_1m smallint,
    circuits_3m smallint,
    circuits_6m smallint,
    circuits_9m smallint,
    circuits_12m smallint,
    vol_day_val bigint,
    vol_avg_1w bigint,
    vol_avg_1m bigint,
    vol_avg_3m bigint,
    vol_avg_6m bigint,
    vol_avg_9m bigint,
    vol_avg_12m bigint,
    median_vol_12m bigint,
    marketcap_cr bigint,
    pe numeric(14,4),
    series character varying,
    regime character varying,
    universe_mask integer DEFAULT 0 NOT NULL,
    top_beta_mask integer DEFAULT 0 NOT NULL,
    top_volatility_mask integer DEFAULT 0 NOT NULL,
    atr_14 numeric(18,4),
    atr_ext_20 numeric(10,4),
    ma50_slope_20 numeric(14,2),
    eff_ratio_63 numeric(10,4),
    max_dd_6m numeric(14,2),
    max_dd_12m numeric(14,2),
    downside_vol_6m numeric(18,10),
    downside_vol_12m numeric(18,10),
    sortino_6m numeric(14,2),
    sortino_12m numeric(14,2),
    underwater_12m numeric(7,2),
    ret_ex_top3_12m numeric(14,2),
    accel_21_105 numeric(18,10),
    accel_21_105_vs numeric(10,4),
    vol_exp_21_126 numeric(10,4),
    vol_persist_20 smallint,
    excess_ret_3m numeric(14,2),
    excess_ret_6m numeric(14,2),
    excess_ret_12m numeric(14,2),
    resid_ret_12m numeric(14,2),
    rs_persist_126 numeric(7,2),
    mom_pctile numeric(7,2),
    rank_persist_20 numeric(7,2),
    nse_mr6 numeric(18,10),
    nse_mr12 numeric(18,10)
);

CREATE TABLE public.fo_backtest_run (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    family character varying(32) NOT NULL,
    params jsonb NOT NULL,
    tier character varying(4) DEFAULT '2E'::character varying NOT NULL,
    caveat text NOT NULL,
    sample_from date NOT NULL,
    sample_to date NOT NULL,
    n integer NOT NULL,
    net_r numeric(10,4),
    gross_r numeric(10,4),
    per_year jsonb DEFAULT '{}'::jsonb NOT NULL,
    slippage_source character varying(8) NOT NULL,
    run_at timestamp with time zone DEFAULT now() NOT NULL,
    git_sha character varying(40),
    CONSTRAINT ck_fo_backtest_run_dates_in_order CHECK ((sample_to >= sample_from)),
    CONSTRAINT ck_fo_backtest_run_n_non_negative CHECK ((n >= 0)),
    CONSTRAINT ck_fo_backtest_run_slippage_source_known CHECK (((slippage_source)::text = ANY ((ARRAY['ASSUMED'::character varying, 'MEASURED'::character varying])::text[])))
);

ALTER TABLE public.fo_backtest_run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.fo_backtest_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.fo_book_config (
    user_id bigint NOT NULL,
    monthly_pause_inr numeric(12,2) DEFAULT '0'::numeric NOT NULL,
    paused_until date,
    paused_reason character varying(32),
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(64),
    CONSTRAINT ck_fo_book_config_monthly_pause_non_negative CHECK ((monthly_pause_inr >= (0)::numeric))
);

CREATE TABLE public.fo_config_audit (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    scope character varying(4) NOT NULL,
    key character varying(64) NOT NULL,
    old_value text,
    new_value text,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    changed_by character varying(64) NOT NULL,
    note text,
    CONSTRAINT ck_fo_config_audit_scope_known CHECK (((scope)::text = ANY ((ARRAY['BOOK'::character varying, 'F1'::character varying, 'F2'::character varying, 'F3'::character varying])::text[])))
);

ALTER TABLE public.fo_config_audit ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.fo_config_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.fo_contract_daily (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
)
PARTITION BY RANGE (trade_date);

CREATE TABLE public.fo_contract_daily_202201 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202202 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202203 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202204 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202205 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202206 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202207 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202208 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202209 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202210 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202211 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202212 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202301 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202302 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202303 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202304 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202305 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202306 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202307 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202308 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202309 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202310 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202311 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202312 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202401 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202402 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202403 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202404 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202405 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202406 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202407 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202408 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202409 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202410 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202411 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202412 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202501 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202502 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202503 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202504 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202505 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202506 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202507 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202508 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202509 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202510 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202511 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202512 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202601 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202602 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202603 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202604 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202605 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202606 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202607 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202608 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202609 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202610 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202611 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202612 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202701 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202702 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202703 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202704 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202705 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202706 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202707 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202708 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202709 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202710 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202711 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_contract_daily_202712 (
    trade_date date NOT NULL,
    instrument character varying(6) NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    settle numeric(18,2) NOT NULL,
    underlying numeric(18,2),
    open_interest bigint,
    oi_change bigint,
    volume bigint,
    turnover numeric(20,2),
    lot_size integer,
    source_key text NOT NULL,
    CONSTRAINT ck_fo_contract_daily_future_iff_xx CHECK ((((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying])::text[])) = ((option_type)::text = 'XX'::text))),
    CONSTRAINT ck_fo_contract_daily_instrument_known CHECK (((instrument)::text = ANY ((ARRAY['FUTSTK'::character varying, 'FUTIDX'::character varying, 'OPTSTK'::character varying, 'OPTIDX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_contract_daily_strike_non_negative CHECK ((strike >= (0)::numeric))
);

CREATE TABLE public.fo_fill (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    leg_id bigint NOT NULL,
    position_id bigint,
    client_id character varying(96),
    filled_at timestamp with time zone NOT NULL,
    side character varying(4) NOT NULL,
    quantity integer NOT NULL,
    price numeric(18,2) NOT NULL,
    simulated boolean NOT NULL,
    sim_method character varying(16) NOT NULL,
    detail jsonb,
    CONSTRAINT ck_fo_fill_price_non_negative CHECK ((price >= (0)::numeric)),
    CONSTRAINT ck_fo_fill_quantity_positive CHECK ((quantity > 0)),
    CONSTRAINT ck_fo_fill_side_known CHECK (((side)::text = ANY ((ARRAY['BUY'::character varying, 'SELL'::character varying])::text[]))),
    CONSTRAINT ck_fo_fill_sim_method_known CHECK (((sim_method)::text = ANY ((ARRAY['DEPTH_LADDER'::character varying, 'LIVE'::character varying])::text[])))
);

ALTER TABLE public.fo_fill ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.fo_fill_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.fo_index_daily (
    underlying character varying(16) NOT NULL,
    trade_date date NOT NULL,
    open numeric(18,2) NOT NULL,
    high numeric(18,2) NOT NULL,
    low numeric(18,2) NOT NULL,
    close numeric(18,2) NOT NULL,
    source character varying(16) DEFAULT 'KITE_HIST'::character varying NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_fo_index_daily_high_not_below_low CHECK ((high >= low)),
    CONSTRAINT ck_fo_index_daily_source_known CHECK (((source)::text = 'KITE_HIST'::text)),
    CONSTRAINT ck_fo_index_daily_underlying_known CHECK (((underlying)::text = ANY ((ARRAY['NIFTY'::character varying, 'BANKNIFTY'::character varying])::text[])))
);

CREATE TABLE public.fo_ingest_day (
    trade_date date NOT NULL,
    status character varying(10) NOT NULL,
    rows_in_file integer,
    rows_kept integer,
    source_key text,
    attempts smallint DEFAULT '0'::smallint NOT NULL,
    last_error text,
    ban_for_session date,
    ban_symbols character varying(32)[],
    ban_source_key text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_fo_ingest_day_attempts_non_negative CHECK ((attempts >= 0)),
    CONSTRAINT ck_fo_ingest_day_status_known CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, 'INGESTED'::character varying, 'MISSING'::character varying])::text[])))
);

CREATE TABLE public.fo_journal (
    position_id bigint NOT NULL,
    user_id bigint NOT NULL,
    sleeve public.fo_sleeve NOT NULL,
    symbol character varying(32) NOT NULL,
    structure character varying(16) NOT NULL,
    opened_on date NOT NULL,
    closed_on date NOT NULL,
    entry_inr numeric(12,2) NOT NULL,
    exit_inr numeric(12,2) NOT NULL,
    gross_pnl_inr numeric(12,2) NOT NULL,
    costs_inr numeric(12,2) NOT NULL,
    net_pnl_inr numeric(12,2) NOT NULL,
    risk_budget_inr numeric(12,2) NOT NULL,
    r_multiple numeric(8,2) NOT NULL,
    closed_reason character varying(32) NOT NULL,
    sessions_held integer NOT NULL,
    rolls smallint DEFAULT '0'::smallint NOT NULL,
    mae_r numeric(8,2),
    mfe_r numeric(8,2),
    simulated boolean NOT NULL,
    sizing_mode character varying(16) NOT NULL,
    detail jsonb,
    CONSTRAINT ck_fo_journal_dates_in_order CHECK ((closed_on >= opened_on)),
    CONSTRAINT ck_fo_journal_rolls_non_negative CHECK ((rolls >= 0)),
    CONSTRAINT ck_fo_journal_sessions_non_negative CHECK ((sessions_held >= 0)),
    CONSTRAINT ck_fo_journal_sizing_mode_known CHECK (((sizing_mode)::text = ANY ((ARRAY['BUDGET'::character varying, 'PAPER_ONE_LOT'::character varying])::text[]))),
    CONSTRAINT ck_fo_journal_structure_known CHECK (((structure)::text = ANY ((ARRAY['IRON_CONDOR'::character varying, 'FUTURE'::character varying, 'CREDIT_SPREAD'::character varying])::text[])))
);

CREATE TABLE public.fo_leg (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    plan_id bigint NOT NULL,
    entry_seq smallint NOT NULL,
    role character varying(12) NOT NULL,
    tradingsymbol character varying(64) NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2),
    option_type character varying(2) NOT NULL,
    side character varying(4) NOT NULL,
    quantity integer NOT NULL,
    limit_price numeric(18,2),
    reference_price numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    status character varying(12) DEFAULT 'PENDING'::character varying NOT NULL,
    filled_qty integer DEFAULT 0 NOT NULL,
    avg_price numeric(18,2),
    simulated boolean DEFAULT true NOT NULL,
    CONSTRAINT ck_fo_leg_entry_seq_positive CHECK ((entry_seq > 0)),
    CONSTRAINT ck_fo_leg_filled_within_qty CHECK (((filled_qty >= 0) AND (filled_qty <= quantity))),
    CONSTRAINT ck_fo_leg_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying, 'XX'::character varying])::text[]))),
    CONSTRAINT ck_fo_leg_quantity_positive CHECK ((quantity > 0)),
    CONSTRAINT ck_fo_leg_role_known CHECK (((role)::text = ANY ((ARRAY['LONG_CALL'::character varying, 'LONG_PUT'::character varying, 'SHORT_CALL'::character varying, 'SHORT_PUT'::character varying, 'LONG_FUTURE'::character varying])::text[]))),
    CONSTRAINT ck_fo_leg_side_known CHECK (((side)::text = ANY ((ARRAY['BUY'::character varying, 'SELL'::character varying])::text[]))),
    CONSTRAINT ck_fo_leg_status_known CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, 'SENT'::character varying, 'FILLED'::character varying, 'PARTIAL'::character varying, 'CANCELLED'::character varying, 'REJECTED'::character varying])::text[])))
);

ALTER TABLE public.fo_leg ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.fo_leg_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.fo_mark (
    position_id bigint NOT NULL,
    trade_date date NOT NULL,
    user_id bigint NOT NULL,
    mark_points numeric(18,2) NOT NULL,
    pnl_inr numeric(12,2) NOT NULL,
    stop_price numeric(18,2),
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.fo_plan (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    plan_id character varying(64) NOT NULL,
    sleeve public.fo_sleeve NOT NULL,
    symbol character varying(32) NOT NULL,
    trade_date date NOT NULL,
    structure character varying(16) NOT NULL,
    kind character varying(8) NOT NULL,
    sizing_mode character varying(16) NOT NULL,
    parent_plan_id character varying(64),
    issued_at timestamp with time zone NOT NULL,
    entry_window_end timestamp with time zone,
    expires_at timestamp with time zone NOT NULL,
    hard_exit_date date,
    credit_points numeric(18,2),
    debit_points numeric(18,2),
    width_points numeric(18,2),
    lots smallint NOT NULL,
    lot_size integer NOT NULL,
    risk_per_lot_inr numeric(12,2),
    risk_budget_inr numeric(12,2),
    max_loss_inr numeric(12,2),
    expected_cost_inr numeric(12,2),
    cost_share numeric(8,4),
    margin_required_inr numeric(12,2),
    iv_rv numeric(10,4),
    status character varying(24) DEFAULT 'ISSUED'::character varying NOT NULL,
    reason text,
    confirmed_at timestamp with time zone,
    detail jsonb,
    CONSTRAINT ck_fo_plan_expires_after_issue CHECK ((expires_at > issued_at)),
    CONSTRAINT ck_fo_plan_kind_known CHECK (((kind)::text = ANY ((ARRAY['ENTRY'::character varying, 'EXIT'::character varying, 'ROLL'::character varying, 'ADD'::character varying])::text[]))),
    CONSTRAINT ck_fo_plan_lot_size_positive CHECK ((lot_size > 0)),
    CONSTRAINT ck_fo_plan_lots_positive CHECK ((lots > 0)),
    CONSTRAINT ck_fo_plan_sizing_mode_known CHECK (((sizing_mode)::text = ANY ((ARRAY['BUDGET'::character varying, 'PAPER_ONE_LOT'::character varying])::text[]))),
    CONSTRAINT ck_fo_plan_structure_known CHECK (((structure)::text = ANY ((ARRAY['IRON_CONDOR'::character varying, 'FUTURE'::character varying, 'CREDIT_SPREAD'::character varying])::text[])))
);

ALTER TABLE public.fo_plan ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.fo_plan_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.fo_position (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    sleeve public.fo_sleeve NOT NULL,
    symbol character varying(32) NOT NULL,
    structure character varying(16) NOT NULL,
    entry_plan_id character varying(64) NOT NULL,
    legs jsonb DEFAULT '[]'::jsonb NOT NULL,
    lots smallint NOT NULL,
    lot_size integer NOT NULL,
    entry_price numeric(18,2),
    entry_credit numeric(18,2),
    max_loss_inr numeric(12,2),
    profit_take_points numeric(18,2),
    loss_close_points numeric(18,2),
    stop_price numeric(18,2),
    gtt_id character varying(32),
    hard_exit_date date,
    next_roll_date date,
    opened_at timestamp with time zone NOT NULL,
    closed_at timestamp with time zone,
    closed_reason character varying(32),
    simulated boolean NOT NULL,
    CONSTRAINT ck_fo_position_lot_size_positive CHECK ((lot_size > 0)),
    CONSTRAINT ck_fo_position_lots_positive CHECK ((lots > 0)),
    CONSTRAINT ck_fo_position_structure_known CHECK (((structure)::text = ANY ((ARRAY['IRON_CONDOR'::character varying, 'FUTURE'::character varying, 'CREDIT_SPREAD'::character varying])::text[])))
);

ALTER TABLE public.fo_position ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.fo_position_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.fo_scan (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    sleeve public.fo_sleeve NOT NULL,
    trade_date date NOT NULL,
    symbol character varying(32) NOT NULL,
    state character varying(24) NOT NULL,
    reasons text[] DEFAULT '{}'::text[] NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    credit numeric(18,2),
    max_loss_per_lot numeric(12,2),
    cost_share numeric(8,4),
    iv numeric(10,6),
    rv20 numeric(10,6),
    iv_rv numeric(10,4),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.fo_scan ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.fo_scan_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.fo_sleeve_config (
    user_id bigint NOT NULL,
    sleeve character varying(4) NOT NULL,
    capital_inr numeric(12,2) DEFAULT '0'::numeric NOT NULL,
    risk_per_trade_pct numeric(5,2) DEFAULT 1.00 NOT NULL,
    max_lots smallint DEFAULT '2'::smallint NOT NULL,
    max_open_positions smallint DEFAULT '1'::smallint NOT NULL,
    paper_enabled boolean DEFAULT true NOT NULL,
    paused_until date,
    paused_reason character varying(32),
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(64),
    CONSTRAINT ck_fo_sleeve_config_capital_non_negative CHECK ((capital_inr >= (0)::numeric)),
    CONSTRAINT ck_fo_sleeve_config_max_lots_positive CHECK ((max_lots > 0)),
    CONSTRAINT ck_fo_sleeve_config_max_open_positive CHECK ((max_open_positions > 0)),
    CONSTRAINT ck_fo_sleeve_config_risk_pct_positive CHECK ((risk_per_trade_pct > (0)::numeric)),
    CONSTRAINT ck_fo_sleeve_config_sleeve_group_known CHECK (((sleeve)::text = ANY ((ARRAY['F1'::character varying, 'F2'::character varying, 'F3'::character varying])::text[])))
);

CREATE TABLE public.fo_spread_sample (
    id bigint NOT NULL,
    trade_date date NOT NULL,
    symbol character varying(32) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    bid numeric(18,2),
    ask numeric(18,2),
    mid numeric(18,2),
    oi bigint,
    taken_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_fo_spread_sample_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[])))
);

ALTER TABLE public.fo_spread_sample ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.fo_spread_sample_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.fo_underlying_daily (
    trade_date date NOT NULL,
    symbol character varying(32) NOT NULL,
    held_expiry date,
    level_o numeric(18,2),
    level_h numeric(18,2),
    level_l numeric(18,2),
    level_c numeric(18,2),
    ret numeric(14,10),
    atr14 numeric(18,2),
    oi_total bigint,
    fut_turnover_20d numeric(20,2),
    iv_atm numeric(10,6),
    rv20 numeric(10,6),
    basis_ann numeric(10,6),
    in_ban boolean DEFAULT false NOT NULL,
    ca_flag boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.fundamental_daily (
    instrument_id bigint NOT NULL,
    date date NOT NULL,
    marketcap_cr bigint,
    pe numeric(14,4),
    pb numeric(14,4),
    div_yield numeric(10,4),
    shares_outstanding bigint
);

CREATE TABLE public.index_bar_daily (
    index_id smallint NOT NULL,
    date date NOT NULL,
    open numeric(14,2) NOT NULL,
    high numeric(14,2) NOT NULL,
    low numeric(14,2) NOT NULL,
    close numeric(14,2) NOT NULL
);

CREATE TABLE public.index_constituents (
    index_id smallint NOT NULL,
    symbol text NOT NULL,
    isin text,
    name text,
    weight numeric(8,4),
    as_of date NOT NULL,
    industry text
);

CREATE TABLE public.index_def (
    id smallint NOT NULL,
    slug character varying NOT NULL,
    name character varying NOT NULL,
    is_universe boolean NOT NULL,
    sort_order smallint NOT NULL
);

CREATE TABLE public.index_master (
    id smallint NOT NULL,
    name text NOT NULL,
    grp text NOT NULL,
    kite_token bigint,
    kite_tradingsymbol text,
    nse_csv_slug text,
    live boolean DEFAULT false NOT NULL,
    last_constituents_at timestamp with time zone,
    sort_order integer DEFAULT 0 NOT NULL
);

ALTER TABLE public.index_master ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.index_master_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.index_member_daily (
    index_id smallint NOT NULL,
    date date NOT NULL,
    instrument_id bigint NOT NULL,
    weight numeric(10,6),
    source character varying DEFAULT 'nse_file'::character varying NOT NULL,
    CONSTRAINT ck_index_member_daily_index_member_source CHECK (((source)::text = ANY ((ARRAY['nse_file'::character varying, 'reconstructed'::character varying, 'derived'::character varying])::text[])))
);

CREATE TABLE public.index_stats_daily (
    index_id smallint NOT NULL,
    date date NOT NULL,
    close numeric(14,2) NOT NULL,
    chg_pct numeric(10,2),
    dma10 numeric(14,2),
    dma20 numeric(14,2),
    dma50 numeric(14,2),
    dma200 numeric(14,2),
    vs20_pct numeric(10,2),
    cross_10_20 boolean,
    ret_5d numeric(10,2),
    ret_30d numeric(10,2),
    ret_1y numeric(10,2),
    hi_52w numeric(14,2),
    lo_52w numeric(14,2),
    dist_52w_hi_pct numeric(10,2),
    ema10 numeric(14,2),
    ema20 numeric(14,2),
    ema_cross boolean
);

CREATE TABLE public.ingest_cursor (
    kind character varying NOT NULL,
    instrument_id bigint NOT NULL,
    window_start date NOT NULL,
    window_end date NOT NULL,
    status character varying NOT NULL,
    rows_written bigint,
    attempts bigint NOT NULL,
    last_error jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_ingest_cursor_ingest_cursor_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'done'::character varying, 'failed'::character varying])::text[]))),
    CONSTRAINT ck_ingest_cursor_ingest_cursor_window_ordered CHECK ((window_end >= window_start))
);

CREATE TABLE public.instrument (
    id bigint NOT NULL,
    exchange_id smallint NOT NULL,
    symbol character varying NOT NULL,
    name character varying NOT NULL,
    isin character varying,
    instrument_type character varying NOT NULL,
    series character varying,
    face_value numeric(12,4),
    lot_size integer,
    listed_on date,
    delisted_on date,
    kite_token bigint,
    is_active boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_instrument_instrument_type_known CHECK (((instrument_type)::text = ANY ((ARRAY['EQ'::character varying, 'ETF'::character varying, 'INDEX'::character varying])::text[])))
);

ALTER TABLE public.instrument ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.instrument_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.instrument_watch_item (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    watched_at timestamp with time zone NOT NULL,
    close_at_watch numeric(18,2)
);

ALTER TABLE public.instrument_watch_item ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.instrument_watch_item_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.invoice_counter (
    series character varying NOT NULL,
    next_value bigint NOT NULL
);

CREATE TABLE public.lv_adoption (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    sleeve character varying(8) NOT NULL,
    position_id bigint NOT NULL,
    symbol text NOT NULL,
    quantity integer NOT NULL,
    avg_cost numeric(18,4) NOT NULL,
    gtt_id character varying(64),
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_lv_adoption_cost_positive CHECK ((avg_cost > (0)::numeric)),
    CONSTRAINT ck_lv_adoption_quantity_positive CHECK ((quantity > 0)),
    CONSTRAINT ck_lv_adoption_sleeve_known CHECK (((sleeve)::text = ANY ((ARRAY['swing'::character varying, 'twt'::character varying, 'vbt'::character varying])::text[])))
);

ALTER TABLE public.lv_adoption ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.lv_adoption_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.lv_exit_order (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    sleeve character varying(8) NOT NULL,
    position_id bigint NOT NULL,
    line_id bigint,
    symbol text NOT NULL,
    broker_order_id character varying(64),
    client_id character varying(128),
    quantity integer NOT NULL,
    reference_price numeric(18,4) NOT NULL,
    state character varying(16) DEFAULT 'SENT'::character varying NOT NULL,
    filled_quantity integer DEFAULT 0 NOT NULL,
    avg_fill_price numeric(18,4),
    reason character varying(24) NOT NULL,
    simulated boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_lv_exit_order_filled_non_negative CHECK ((filled_quantity >= 0)),
    CONSTRAINT ck_lv_exit_order_filled_within_quantity CHECK ((filled_quantity <= quantity)),
    CONSTRAINT ck_lv_exit_order_quantity_positive CHECK ((quantity > 0)),
    CONSTRAINT ck_lv_exit_order_sleeve_known CHECK (((sleeve)::text = ANY ((ARRAY['swing'::character varying, 'twt'::character varying, 'vbt'::character varying])::text[]))),
    CONSTRAINT ck_lv_exit_order_state_known CHECK (((state)::text = ANY ((ARRAY['SENT'::character varying, 'PARTIAL'::character varying, 'FILLED'::character varying, 'CANCELLED'::character varying, 'REJECTED'::character varying])::text[])))
);

ALTER TABLE public.lv_exit_order ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.lv_exit_order_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.lv_heartbeat (
    user_id bigint NOT NULL,
    process character varying(32) NOT NULL,
    state character varying(24) NOT NULL,
    detail text DEFAULT ''::text NOT NULL,
    at timestamp with time zone NOT NULL
);

CREATE TABLE public.lv_protection_issue (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    sleeve character varying(8) NOT NULL,
    position_id bigint NOT NULL,
    symbol text NOT NULL,
    kind character varying(24) NOT NULL,
    detail text NOT NULL,
    seen_at timestamp with time zone NOT NULL,
    resolved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_lv_protection_issue_kind_known CHECK (((kind)::text = ANY ((ARRAY['NAKED'::character varying, 'GTT_MISSING'::character varying, 'GTT_OVERSIZED'::character varying, 'GTT_UNDERSIZED'::character varying, 'GTT_TRIGGERED_UNFILLED'::character varying, 'EXTERNAL_EXIT'::character varying, 'STOP_REJECTED'::character varying])::text[]))),
    CONSTRAINT ck_lv_protection_issue_sleeve_known CHECK (((sleeve)::text = ANY ((ARRAY['swing'::character varying, 'twt'::character varying, 'vbt'::character varying])::text[])))
);

ALTER TABLE public.lv_protection_issue ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.lv_protection_issue_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.mtf_leverage (
    as_of date NOT NULL,
    symbol text NOT NULL,
    eligible boolean NOT NULL,
    leverage numeric(6,2),
    reason text,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.ohlcv_daily (
    instrument_id bigint NOT NULL,
    date date NOT NULL,
    open numeric(18,4) NOT NULL,
    high numeric(18,4) NOT NULL,
    low numeric(18,4) NOT NULL,
    close numeric(18,4) NOT NULL,
    volume bigint NOT NULL,
    close_raw numeric(18,4) NOT NULL,
    volume_raw bigint NOT NULL,
    turnover numeric(20,2),
    adj_factor numeric(18,10) DEFAULT '1'::numeric NOT NULL,
    upper_circuit numeric(18,4),
    lower_circuit numeric(18,4),
    source character varying NOT NULL,
    open_raw numeric(18,4),
    high_raw numeric(18,4),
    low_raw numeric(18,4),
    CONSTRAINT ck_ohlcv_daily_ohlcv_daily_source CHECK (((source)::text = ANY ((ARRAY['kite'::character varying, 'nse'::character varying, 'kite_adjusted'::character varying])::text[])))
);

CREATE TABLE public.op_backtest_run (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    sleeve public.op_sleeve NOT NULL,
    tier smallint NOT NULL,
    params_json jsonb NOT NULL,
    date_from date NOT NULL,
    date_to date NOT NULL,
    sessions integer NOT NULL,
    signals integer NOT NULL,
    traded integer NOT NULL,
    skipped_by_reason_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    win_rate numeric(8,4),
    expectancy_r numeric(8,4),
    net_pnl_inr numeric(12,2),
    max_drawdown_r numeric(8,2),
    caveats text NOT NULL,
    ran_at timestamp with time zone DEFAULT now() NOT NULL,
    git_sha character varying(40),
    CONSTRAINT ck_op_backtest_run_counts_non_negative CHECK (((sessions >= 0) AND (signals >= 0) AND (traded >= 0))),
    CONSTRAINT ck_op_backtest_run_dates_in_order CHECK ((date_to >= date_from)),
    CONSTRAINT ck_op_backtest_run_tier_known CHECK ((tier = ANY (ARRAY[1, 2, 3])))
);

ALTER TABLE public.op_backtest_run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.op_backtest_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.op_book_config (
    user_id bigint NOT NULL,
    underlying character varying(16) DEFAULT 'NIFTY'::character varying NOT NULL,
    account_inr numeric(12,2) DEFAULT '0'::numeric NOT NULL,
    margin_pool_inr numeric(12,2) DEFAULT '0'::numeric NOT NULL,
    daily_loss_limit_inr numeric(12,2) DEFAULT '0'::numeric NOT NULL,
    monthly_pause_inr numeric(12,2) DEFAULT '0'::numeric NOT NULL,
    paused_until date,
    paused_reason character varying(32),
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(64),
    CONSTRAINT ck_op_book_config_account_non_negative CHECK ((account_inr >= (0)::numeric)),
    CONSTRAINT ck_op_book_config_daily_limit_non_negative CHECK ((daily_loss_limit_inr >= (0)::numeric)),
    CONSTRAINT ck_op_book_config_margin_pool_non_negative CHECK ((margin_pool_inr >= (0)::numeric)),
    CONSTRAINT ck_op_book_config_monthly_pause_non_negative CHECK ((monthly_pause_inr >= (0)::numeric)),
    CONSTRAINT ck_op_book_config_underlying_nifty_only CHECK (((underlying)::text = 'NIFTY'::text))
);

CREATE TABLE public.op_chain_snapshot (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
)
PARTITION BY RANGE (ts);

CREATE TABLE public.op_chain_snapshot_202609 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202610 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202611 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202612 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202701 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202702 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202703 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202704 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202705 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202706 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202707 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202708 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202709 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202710 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202711 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_chain_snapshot_202712 (
    ts timestamp with time zone NOT NULL,
    instrument_token bigint NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    spot numeric(18,2),
    bid numeric(18,2),
    ask numeric(18,2),
    last numeric(18,2),
    bid_qty integer,
    ask_qty integer,
    depth_json jsonb,
    volume bigint,
    oi bigint,
    forward numeric(18,2),
    iv numeric(10,6),
    delta numeric(10,6),
    gamma numeric(10,6),
    theta numeric(10,6),
    vega numeric(10,6),
    greeks_model character varying(32),
    source character varying(8) DEFAULT 'QUOTE'::character varying NOT NULL,
    CONSTRAINT ck_op_chain_snapshot_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_chain_snapshot_source_known CHECK (((source)::text = ANY ((ARRAY['QUOTE'::character varying, 'VENDOR'::character varying])::text[])))
);

CREATE TABLE public.op_config_audit (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    scope character varying(4) NOT NULL,
    key character varying(64) NOT NULL,
    old_value text,
    new_value text,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    changed_by character varying(64) NOT NULL,
    note text,
    CONSTRAINT ck_op_config_audit_scope_known CHECK (((scope)::text = ANY ((ARRAY['BOOK'::character varying, 'O1M'::character varying, 'O1W'::character varying, 'O2'::character varying, 'O3'::character varying])::text[])))
);

ALTER TABLE public.op_config_audit ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.op_config_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.op_contract (
    instrument_token bigint NOT NULL,
    tradingsymbol character varying(64) NOT NULL,
    underlying character varying(16) NOT NULL,
    expiry date NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    lot_size integer NOT NULL,
    tick_size numeric(8,2) NOT NULL,
    first_seen date NOT NULL,
    last_seen date NOT NULL,
    expired boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_op_contract_lot_size_positive CHECK ((lot_size > 0)),
    CONSTRAINT ck_op_contract_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_contract_seen_in_order CHECK ((last_seen >= first_seen)),
    CONSTRAINT ck_op_contract_strike_positive CHECK ((strike > (0)::numeric)),
    CONSTRAINT ck_op_contract_tick_size_positive CHECK ((tick_size > (0)::numeric))
);

CREATE TABLE public.op_event_day (
    user_id bigint NOT NULL,
    date date NOT NULL,
    reason character varying(24) NOT NULL,
    source character varying(8) NOT NULL,
    source_url text,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_op_event_day_reason_known CHECK (((reason)::text = ANY ((ARRAY['RBI_POLICY'::character varying, 'UNION_BUDGET'::character varying, 'ELECTION_RESULT'::character varying, 'MANUAL'::character varying])::text[]))),
    CONSTRAINT ck_op_event_day_source_known CHECK (((source)::text = ANY ((ARRAY['SEED'::character varying, 'USER'::character varying])::text[])))
);

CREATE TABLE public.op_expiry (
    underlying character varying(16) NOT NULL,
    expiry_date date NOT NULL,
    kind character varying(8) NOT NULL,
    lot_size integer NOT NULL,
    first_seen date NOT NULL,
    seen_on date NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ck_op_expiry_kind_known CHECK (((kind)::text = ANY ((ARRAY['WEEKLY'::character varying, 'MONTHLY'::character varying])::text[]))),
    CONSTRAINT ck_op_expiry_lot_size_positive CHECK ((lot_size > 0)),
    CONSTRAINT ck_op_expiry_seen_in_order CHECK ((seen_on >= first_seen))
);

CREATE TABLE public.op_fill (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    leg_id bigint NOT NULL,
    order_id bigint,
    filled_at timestamp with time zone NOT NULL,
    quantity integer NOT NULL,
    price numeric(18,2) NOT NULL,
    simulated boolean NOT NULL,
    sim_method character varying(16) NOT NULL,
    detail jsonb,
    CONSTRAINT ck_op_fill_price_non_negative CHECK ((price >= (0)::numeric)),
    CONSTRAINT ck_op_fill_quantity_positive CHECK ((quantity > 0)),
    CONSTRAINT ck_op_fill_sim_method_known CHECK (((sim_method)::text = ANY ((ARRAY['DEPTH_LADDER'::character varying, 'LIVE'::character varying])::text[])))
);

ALTER TABLE public.op_fill ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.op_fill_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.op_index_minute (
    instrument_id bigint NOT NULL,
    ts timestamp with time zone NOT NULL,
    open numeric(18,2) NOT NULL,
    high numeric(18,2) NOT NULL,
    low numeric(18,2) NOT NULL,
    close numeric(18,2) NOT NULL,
    source character varying(16) NOT NULL,
    CONSTRAINT ck_op_index_minute_high_not_below_low CHECK ((high >= low)),
    CONSTRAINT ck_op_index_minute_source_known CHECK (((source)::text = ANY ((ARRAY['KITE_HIST'::character varying, 'TICKS'::character varying])::text[])))
);

CREATE TABLE public.op_journal (
    session_id bigint NOT NULL,
    user_id bigint NOT NULL,
    sleeve public.op_sleeve NOT NULL,
    trade_date date NOT NULL,
    expiry_used date NOT NULL,
    structure character varying(16) NOT NULL,
    entry_inr numeric(12,2) NOT NULL,
    exit_inr numeric(12,2) NOT NULL,
    gross_pnl_inr numeric(12,2) NOT NULL,
    costs_inr numeric(12,2) NOT NULL,
    net_pnl_inr numeric(12,2) NOT NULL,
    risk_budget_inr numeric(12,2) NOT NULL,
    r_multiple numeric(8,2) NOT NULL,
    closed_reason character varying(32) NOT NULL,
    minutes_held integer NOT NULL,
    mae_r numeric(8,2),
    mfe_r numeric(8,2),
    simulated boolean NOT NULL,
    sizing_mode character varying(16) NOT NULL,
    half_size boolean DEFAULT false NOT NULL,
    detail jsonb,
    CONSTRAINT ck_op_journal_minutes_non_negative CHECK ((minutes_held >= 0)),
    CONSTRAINT ck_op_journal_sizing_mode_known CHECK (((sizing_mode)::text = ANY ((ARRAY['BUDGET'::character varying, 'PAPER_ONE_LOT'::character varying])::text[]))),
    CONSTRAINT ck_op_journal_structure_known CHECK (((structure)::text = ANY ((ARRAY['IRON_CONDOR'::character varying, 'LONG_OPTION'::character varying, 'DEBIT_SPREAD'::character varying])::text[])))
);

CREATE TABLE public.op_leg (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    plan_id bigint NOT NULL,
    seq smallint NOT NULL,
    role character varying(12) NOT NULL,
    tradingsymbol character varying(64) NOT NULL,
    instrument_token bigint NOT NULL,
    strike numeric(18,2) NOT NULL,
    option_type character varying(2) NOT NULL,
    side character varying(4) NOT NULL,
    quantity integer NOT NULL,
    limit_price numeric(18,2),
    iv_at_plan numeric(10,6),
    delta_at_plan numeric(10,6),
    bid numeric(18,2),
    ask numeric(18,2),
    depth_json jsonb,
    status character varying(12) DEFAULT 'PENDING'::character varying NOT NULL,
    filled_qty integer DEFAULT 0 NOT NULL,
    avg_price numeric(18,2),
    simulated boolean DEFAULT true NOT NULL,
    CONSTRAINT ck_op_leg_filled_within_qty CHECK (((filled_qty >= 0) AND (filled_qty <= quantity))),
    CONSTRAINT ck_op_leg_option_type_known CHECK (((option_type)::text = ANY ((ARRAY['CE'::character varying, 'PE'::character varying])::text[]))),
    CONSTRAINT ck_op_leg_quantity_positive CHECK ((quantity > 0)),
    CONSTRAINT ck_op_leg_role_known CHECK (((role)::text = ANY ((ARRAY['LONG_CALL'::character varying, 'LONG_PUT'::character varying, 'SHORT_CALL'::character varying, 'SHORT_PUT'::character varying])::text[]))),
    CONSTRAINT ck_op_leg_seq_positive CHECK ((seq > 0)),
    CONSTRAINT ck_op_leg_side_known CHECK (((side)::text = ANY ((ARRAY['BUY'::character varying, 'SELL'::character varying])::text[]))),
    CONSTRAINT ck_op_leg_status_known CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, 'SENT'::character varying, 'FILLED'::character varying, 'PARTIAL'::character varying, 'CANCELLED'::character varying, 'REJECTED'::character varying])::text[])))
);

ALTER TABLE public.op_leg ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.op_leg_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.op_order (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    leg_id bigint NOT NULL,
    client_id character varying(96) NOT NULL,
    product character varying(8) DEFAULT 'MIS'::character varying NOT NULL,
    exchange character varying(8) DEFAULT 'NFO'::character varying NOT NULL,
    side character varying(4) NOT NULL,
    quantity integer NOT NULL,
    limit_price numeric(18,2),
    broker_order_id character varying(64),
    gateway_status character varying(24),
    gateway_result jsonb,
    simulated boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_op_order_exchange_nfo_only CHECK (((exchange)::text = 'NFO'::text)),
    CONSTRAINT ck_op_order_product_mis_only CHECK (((product)::text = 'MIS'::text)),
    CONSTRAINT ck_op_order_quantity_positive CHECK ((quantity > 0)),
    CONSTRAINT ck_op_order_side_known CHECK (((side)::text = ANY ((ARRAY['BUY'::character varying, 'SELL'::character varying])::text[])))
);

ALTER TABLE public.op_order ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.op_order_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.op_plan (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    plan_id character varying(64) NOT NULL,
    session_id bigint NOT NULL,
    sleeve public.op_sleeve NOT NULL,
    structure character varying(16) NOT NULL,
    kind character varying(8) NOT NULL,
    sizing_mode character varying(16) NOT NULL,
    issued_at timestamp with time zone NOT NULL,
    entry_window_end timestamp with time zone,
    expires_at timestamp with time zone NOT NULL,
    credit_points numeric(18,2),
    debit_points numeric(18,2),
    width_points numeric(18,2),
    lots smallint NOT NULL,
    lot_size smallint NOT NULL,
    risk_per_lot_inr numeric(12,2),
    risk_budget_inr numeric(12,2),
    max_loss_inr numeric(12,2),
    profit_target_inr numeric(12,2),
    stop_inr numeric(12,2),
    expected_cost_inr numeric(12,2),
    cost_share numeric(8,4),
    margin_required_inr numeric(12,2),
    status character varying(12) DEFAULT 'ISSUED'::character varying NOT NULL,
    rejected_code character varying(48),
    confirmed_at timestamp with time zone,
    detail jsonb,
    CONSTRAINT ck_op_plan_expires_after_issue CHECK ((expires_at > issued_at)),
    CONSTRAINT ck_op_plan_kind_known CHECK (((kind)::text = ANY ((ARRAY['ENTRY'::character varying, 'EXIT'::character varying])::text[]))),
    CONSTRAINT ck_op_plan_lot_size_positive CHECK ((lot_size > 0)),
    CONSTRAINT ck_op_plan_lots_positive CHECK ((lots > 0)),
    CONSTRAINT ck_op_plan_sizing_mode_known CHECK (((sizing_mode)::text = ANY ((ARRAY['BUDGET'::character varying, 'PAPER_ONE_LOT'::character varying])::text[]))),
    CONSTRAINT ck_op_plan_status_known CHECK (((status)::text = ANY ((ARRAY['ISSUED'::character varying, 'CONFIRMED'::character varying, 'LAPSED'::character varying, 'REJECTED'::character varying])::text[]))),
    CONSTRAINT ck_op_plan_structure_known CHECK (((structure)::text = ANY ((ARRAY['IRON_CONDOR'::character varying, 'LONG_OPTION'::character varying, 'DEBIT_SPREAD'::character varying])::text[])))
);

ALTER TABLE public.op_plan ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.op_plan_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.op_position (
    session_id bigint NOT NULL,
    user_id bigint NOT NULL,
    leg_ids bigint[] NOT NULL,
    entry_points numeric(18,2) NOT NULL,
    entry_inr numeric(12,2) NOT NULL,
    lots smallint NOT NULL,
    opened_at timestamp with time zone NOT NULL,
    hard_exit_at timestamp with time zone NOT NULL,
    peak_value numeric(18,2),
    last_mark_points numeric(18,2),
    last_mark_at timestamp with time zone,
    exit_plan_id character varying(64),
    closed_at timestamp with time zone,
    simulated boolean NOT NULL,
    trough_value numeric(18,2),
    CONSTRAINT ck_op_position_lots_positive CHECK ((lots > 0))
);

CREATE TABLE public.op_scan (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    sleeve public.op_sleeve NOT NULL,
    trade_date date NOT NULL,
    ts timestamp with time zone NOT NULL,
    state character varying(24) NOT NULL,
    reasons character varying(48)[] DEFAULT '{}'::character varying[] NOT NULL,
    numbers jsonb DEFAULT '{}'::jsonb NOT NULL,
    candidates jsonb DEFAULT '[]'::jsonb NOT NULL,
    as_of_minute timestamp with time zone,
    stale boolean DEFAULT false NOT NULL
);

ALTER TABLE public.op_scan ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.op_scan_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.op_session (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    sleeve public.op_sleeve NOT NULL,
    trade_date date NOT NULL,
    expiry_used date,
    mode character varying(8) NOT NULL,
    state character varying(16) DEFAULT 'OBSERVING'::character varying NOT NULL,
    numbers jsonb DEFAULT '{}'::jsonb NOT NULL,
    verdict character varying(16),
    skip_reasons character varying(48)[] DEFAULT '{}'::character varying[] NOT NULL,
    plan_id character varying(64),
    closed_reason character varying(32),
    pnl_inr numeric(12,2),
    pnl_r numeric(8,2),
    slot_holder character varying(4),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_op_session_mode_known CHECK (((mode)::text = ANY ((ARRAY['PAPER'::character varying, 'LIVE'::character varying])::text[]))),
    CONSTRAINT ck_op_session_slot_holder_known CHECK (((slot_holder IS NULL) OR ((slot_holder)::text = ANY ((ARRAY['O1M'::character varying, 'O1W'::character varying, 'O2'::character varying, 'O3A'::character varying, 'O3B'::character varying])::text[])))),
    CONSTRAINT ck_op_session_state_known CHECK (((state)::text = ANY ((ARRAY['OBSERVING'::character varying, 'SKIPPED'::character varying, 'PLANNED'::character varying, 'LAPSED'::character varying, 'CONFIRMED'::character varying, 'OPEN'::character varying, 'CLOSED'::character varying])::text[])))
);

ALTER TABLE public.op_session ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.op_session_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.op_sleeve_config (
    user_id bigint NOT NULL,
    sleeve character varying(4) NOT NULL,
    sleeve_capital_inr numeric(12,2) DEFAULT '0'::numeric NOT NULL,
    risk_per_trade_pct numeric(5,2) DEFAULT 0.50 NOT NULL,
    max_lots smallint DEFAULT '2'::smallint NOT NULL,
    paper_enabled boolean DEFAULT true NOT NULL,
    hard_exit_time time without time zone DEFAULT '14:30:00'::time without time zone NOT NULL,
    paused_until date,
    paused_reason character varying(32),
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(64),
    CONSTRAINT ck_op_sleeve_config_capital_non_negative CHECK ((sleeve_capital_inr >= (0)::numeric)),
    CONSTRAINT ck_op_sleeve_config_max_lots_positive CHECK ((max_lots > 0)),
    CONSTRAINT ck_op_sleeve_config_risk_pct_positive CHECK ((risk_per_trade_pct > (0)::numeric)),
    CONSTRAINT ck_op_sleeve_config_sleeve_group_known CHECK (((sleeve)::text = ANY ((ARRAY['O1M'::character varying, 'O1W'::character varying, 'O2'::character varying, 'O3'::character varying])::text[])))
);

CREATE TABLE public.payment (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    subscription_id bigint,
    amount_inr numeric(12,2) NOT NULL,
    gst_inr numeric(12,2),
    status character varying NOT NULL,
    razorpay_payment_id character varying,
    invoice_number character varying,
    invoice_pdf_key character varying,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    razorpay_order_id character varying,
    invoice_date date,
    taxable_inr numeric(12,2),
    cgst_inr numeric(12,2),
    sgst_inr numeric(12,2),
    igst_inr numeric(12,2),
    gst_rate numeric(5,2),
    place_of_supply character varying,
    customer_gstin character varying,
    sac_code character varying,
    CONSTRAINT ck_payment_payment_status CHECK (((status)::text = ANY ((ARRAY['created'::character varying, 'captured'::character varying, 'failed'::character varying, 'refunded'::character varying])::text[])))
);

ALTER TABLE public.payment ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.payment_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.pipeline_run (
    id bigint NOT NULL,
    trade_date date NOT NULL,
    status character varying NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    data_version bigint
);

ALTER TABLE public.pipeline_run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pipeline_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.pipeline_run_step (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    step character varying NOT NULL,
    status character varying NOT NULL,
    rows_in bigint,
    rows_out bigint,
    duration_ms integer,
    error jsonb
);

ALTER TABLE public.pipeline_run_step ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pipeline_run_step_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.plan (
    id smallint NOT NULL,
    code character varying NOT NULL,
    price_inr numeric(12,2) NOT NULL,
    "interval" character varying,
    features jsonb NOT NULL,
    CONSTRAINT ck_plan_plan_interval CHECK ((("interval" IS NULL) OR (("interval")::text = ANY ((ARRAY['month'::character varying, 'year'::character varying])::text[]))))
);

CREATE TABLE public.portfolio (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    name character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    parent_id bigint,
    broker_account_id bigint,
    kind character varying NOT NULL,
    source character varying NOT NULL,
    started_on date NOT NULL,
    benchmark_index_id smallint,
    is_broker_pile boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_portfolio_portfolio_kind_known CHECK (((kind)::text = ANY ((ARRAY['CAPITAL'::character varying, 'MONITORING'::character varying])::text[]))),
    CONSTRAINT ck_portfolio_portfolio_parent_not_self CHECK (((parent_id IS NULL) OR (parent_id <> id))),
    CONSTRAINT ck_portfolio_portfolio_source_known CHECK (((source)::text = ANY ((ARRAY['SUBSCRIBED'::character varying, 'MY_SCREEN'::character varying, 'MY_STRATEGY'::character varying, 'HOLDING_GROUP'::character varying])::text[])))
);

CREATE TABLE public.portfolio_cash_flow (
    id bigint NOT NULL,
    broker_account_id bigint NOT NULL,
    portfolio_id bigint,
    kind character varying NOT NULL,
    amount numeric(20,2) NOT NULL,
    occurred_on date NOT NULL,
    instrument_id bigint,
    quantity numeric(20,4),
    note character varying,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_portfolio_cash_flow_portfolio_cash_flow_external_has_8c71 CHECK (((((kind)::text = ANY ((ARRAY['EXTERNAL_DEPOSIT'::character varying, 'EXTERNAL_WITHDRAWAL'::character varying])::text[])) AND (portfolio_id IS NULL)) OR (((kind)::text <> ALL ((ARRAY['EXTERNAL_DEPOSIT'::character varying, 'EXTERNAL_WITHDRAWAL'::character varying])::text[])) AND (portfolio_id IS NOT NULL)))),
    CONSTRAINT ck_portfolio_cash_flow_portfolio_cash_flow_kind_known CHECK (((kind)::text = ANY ((ARRAY['EXTERNAL_DEPOSIT'::character varying, 'EXTERNAL_WITHDRAWAL'::character varying, 'ASSIGN'::character varying, 'RELEASE'::character varying, 'BUY'::character varying, 'SELL'::character varying, 'DIVIDEND'::character varying])::text[])))
);

ALTER TABLE public.portfolio_cash_flow ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.portfolio_cash_flow_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.portfolio_holding (
    portfolio_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    quantity numeric(20,4),
    avg_price numeric(18,4),
    added_on date NOT NULL,
    broker_account_id bigint NOT NULL,
    portfolio_kind character varying NOT NULL,
    first_bought_on date,
    history_source character varying DEFAULT 'NONE'::character varying NOT NULL,
    CONSTRAINT ck_portfolio_holding_portfolio_holding_history_source_known CHECK (((history_source)::text = ANY ((ARRAY['NONE'::character varying, 'CAS'::character varying, 'BROKER'::character varying, 'MANUAL'::character varying])::text[]))),
    CONSTRAINT ck_portfolio_holding_portfolio_holding_quantity_not_negative CHECK (((quantity IS NULL) OR (quantity >= (0)::numeric)))
);

ALTER TABLE public.portfolio ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.portfolio_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.portfolio_nav_daily (
    portfolio_id bigint,
    user_id bigint NOT NULL,
    date date NOT NULL,
    market_value numeric(20,2) NOT NULL,
    cash numeric(20,2) DEFAULT '0'::numeric NOT NULL,
    net_flow numeric(20,2) DEFAULT '0'::numeric NOT NULL,
    pending_reconciliation boolean DEFAULT false NOT NULL
);

CREATE TABLE public.portfolio_rebalance (
    id bigint NOT NULL,
    portfolio_id bigint NOT NULL,
    screen_id bigint,
    as_of date NOT NULL,
    data_version bigint,
    top_n integer NOT NULL,
    hold_buffer integer NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.portfolio_rebalance_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.portfolio_rebalance_id_seq OWNED BY public.portfolio_rebalance.id;

CREATE TABLE public.portfolio_sleeve (
    id bigint NOT NULL,
    portfolio_id bigint NOT NULL,
    name character varying NOT NULL,
    kind character varying NOT NULL,
    screen_id bigint,
    capital numeric(18,2) DEFAULT '0'::numeric NOT NULL,
    top_n smallint DEFAULT '15'::smallint NOT NULL,
    sort_order smallint DEFAULT '0'::smallint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    basket_id bigint,
    CONSTRAINT ck_portfolio_sleeve_portfolio_sleeve_capital_non_negative CHECK ((capital >= (0)::numeric)),
    CONSTRAINT ck_portfolio_sleeve_portfolio_sleeve_kind CHECK (((kind)::text = ANY ((ARRAY['screen'::character varying, 'manual'::character varying, 'basket'::character varying])::text[]))),
    CONSTRAINT ck_portfolio_sleeve_portfolio_sleeve_source CHECK (((((kind)::text = 'screen'::text) AND (screen_id IS NOT NULL) AND (basket_id IS NULL)) OR (((kind)::text = 'manual'::text) AND (screen_id IS NULL) AND (basket_id IS NULL)) OR (((kind)::text = 'basket'::text) AND (basket_id IS NOT NULL) AND (screen_id IS NULL)))),
    CONSTRAINT ck_portfolio_sleeve_portfolio_sleeve_top_n CHECK (((top_n >= 1) AND (top_n <= 100)))
);

CREATE SEQUENCE public.portfolio_sleeve_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.portfolio_sleeve_id_seq OWNED BY public.portfolio_sleeve.id;

CREATE TABLE public.reconciliation_item (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    broker_account_id bigint NOT NULL,
    quantity numeric(20,4) NOT NULL,
    reason character varying NOT NULL,
    state character varying DEFAULT 'OPEN'::character varying NOT NULL,
    suggested_portfolio_id bigint,
    resolved_portfolio_id bigint,
    detected_on date NOT NULL,
    resolved_at timestamp with time zone,
    CONSTRAINT ck_reconciliation_item_reconciliation_item_reason_known CHECK (((reason)::text = ANY ((ARRAY['UNALLOCATED_HOLDING'::character varying, 'QUANTITY_MISMATCH'::character varying, 'UNKNOWN_INFLOW'::character varying, 'SPLIT_HOLDING'::character varying])::text[]))),
    CONSTRAINT ck_reconciliation_item_reconciliation_item_resolved_nam_5e5c CHECK (((((state)::text = 'RESOLVED'::text) AND (resolved_portfolio_id IS NOT NULL)) OR (((state)::text <> 'RESOLVED'::text) AND (resolved_portfolio_id IS NULL)))),
    CONSTRAINT ck_reconciliation_item_reconciliation_item_state_known CHECK (((state)::text = ANY ((ARRAY['OPEN'::character varying, 'RESOLVED'::character varying, 'DISMISSED'::character varying])::text[])))
);

ALTER TABLE public.reconciliation_item ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.reconciliation_item_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.refresh_token (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    family_id character varying NOT NULL,
    token_hash character varying NOT NULL,
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    revoked_reason character varying,
    replaced_by_id bigint
);

ALTER TABLE public.refresh_token ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.refresh_token_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.risk_ledger (
    user_id bigint NOT NULL,
    day date NOT NULL,
    payload jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.screen (
    id bigint NOT NULL,
    public_id character varying NOT NULL,
    user_id bigint,
    name character varying NOT NULL,
    definition jsonb NOT NULL,
    columns jsonb DEFAULT '[]'::jsonb NOT NULL,
    is_example boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.screen_alert (
    id bigint NOT NULL,
    public_id character varying NOT NULL,
    user_id bigint NOT NULL,
    screen_id bigint NOT NULL,
    frequency character varying NOT NULL,
    weekday smallint,
    top_n integer,
    min_move integer DEFAULT 1 NOT NULL,
    digest boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    unsubscribe_token_hash character varying NOT NULL,
    last_run_id bigint,
    last_sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_screen_alert_ck_screen_alert_screen_alert_frequency CHECK (((frequency)::text = ANY ((ARRAY['daily'::character varying, 'weekly'::character varying])::text[]))),
    CONSTRAINT ck_screen_alert_ck_screen_alert_screen_alert_min_move CHECK ((min_move >= 1))
);

CREATE TABLE public.screen_alert_delivery (
    id bigint NOT NULL,
    alert_id bigint NOT NULL,
    as_of date NOT NULL,
    previous_as_of date,
    status character varying NOT NULL,
    entry_count integer DEFAULT 0 NOT NULL,
    exit_count integer DEFAULT 0 NOT NULL,
    change_count integer DEFAULT 0 NOT NULL,
    detail jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_screen_alert_delivery_ck_screen_alert_delivery_scree_5354 CHECK (((status)::text = ANY ((ARRAY['sent'::character varying, 'skipped'::character varying, 'failed'::character varying])::text[])))
);

ALTER TABLE public.screen_alert_delivery ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.screen_alert_delivery_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

ALTER TABLE public.screen_alert ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.screen_alert_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

ALTER TABLE public.screen ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.screen_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.screen_run (
    id bigint NOT NULL,
    screen_id bigint NOT NULL,
    as_of date NOT NULL,
    definition_hash character varying NOT NULL,
    result_count integer NOT NULL,
    results jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.screen_run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.screen_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.sector_index_map (
    industry text NOT NULL,
    index_name text NOT NULL
);

CREATE TABLE public.stock_regime_signal (
    as_of date NOT NULL,
    symbol text NOT NULL,
    signal text NOT NULL,
    fallback boolean DEFAULT false NOT NULL,
    evidence jsonb NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_stock_regime_signal_signal CHECK ((signal = ANY (ARRAY['GREEN'::text, 'RED'::text])))
);

CREATE TABLE public.subscription (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    plan_id smallint NOT NULL,
    status character varying NOT NULL,
    started_at timestamp with time zone NOT NULL,
    current_period_end timestamp with time zone,
    razorpay_subscription_id character varying,
    razorpay_customer_id character varying,
    CONSTRAINT ck_subscription_subscription_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'past_due'::character varying, 'cancelled'::character varying, 'expired'::character varying])::text[])))
);

ALTER TABLE public.subscription ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.subscription_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.sw_backtest_run (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    params jsonb NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    stats jsonb,
    error text,
    CONSTRAINT ck_sw_backtest_run_finished_after_started CHECK (((finished_at IS NULL) OR (finished_at >= started_at)))
);

ALTER TABLE public.sw_backtest_run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.sw_backtest_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.sw_catalyst (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    headline text NOT NULL,
    published_at timestamp with time zone,
    url text NOT NULL,
    source character varying NOT NULL,
    earnings_date date,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_sw_catalyst_source_known CHECK (((source)::text = ANY ((ARRAY['NSE_ANNOUNCEMENT'::character varying, 'NSE_EVENT_CALENDAR'::character varying])::text[])))
);

ALTER TABLE public.sw_catalyst ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.sw_catalyst_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.sw_config (
    user_id bigint NOT NULL,
    sleeve_capital_inr numeric(20,2) DEFAULT '0'::numeric NOT NULL,
    risk_per_trade_pct numeric(5,3) DEFAULT 0.500 NOT NULL,
    max_position_pct numeric(5,2) DEFAULT 20.00 NOT NULL,
    max_open_positions smallint DEFAULT '10'::smallint NOT NULL,
    or_window_minutes smallint DEFAULT '5'::smallint NOT NULL,
    stop_mode character varying DEFAULT 'LOW_OF_DAY'::character varying NOT NULL,
    adr_min_pct numeric(10,2) DEFAULT 4.00 NOT NULL,
    turnover_min_inr numeric(20,2) DEFAULT '50000000'::numeric NOT NULL,
    price_min numeric(18,2) DEFAULT 20.00 NOT NULL,
    exposure_level smallint DEFAULT '0'::smallint NOT NULL,
    first_live_sessions_left smallint DEFAULT '5'::smallint NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying,
    sleeve_peak_inr numeric(20,2),
    drawdown_pct numeric(10,2) DEFAULT '0'::numeric NOT NULL,
    drawdown_locked boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_sw_config_capital_non_negative CHECK ((sleeve_capital_inr >= (0)::numeric)),
    CONSTRAINT ck_sw_config_exposure_level_range CHECK (((exposure_level >= 0) AND (exposure_level <= 3))),
    CONSTRAINT ck_sw_config_first_live_sessions_non_negative CHECK ((first_live_sessions_left >= 0)),
    CONSTRAINT ck_sw_config_max_positions_positive CHECK ((max_open_positions > 0)),
    CONSTRAINT ck_sw_config_or_window_known CHECK ((or_window_minutes = ANY (ARRAY[1, 5, 60]))),
    CONSTRAINT ck_sw_config_position_pct_positive CHECK ((max_position_pct > (0)::numeric)),
    CONSTRAINT ck_sw_config_risk_positive CHECK ((risk_per_trade_pct > (0)::numeric)),
    CONSTRAINT ck_sw_config_stop_mode_known CHECK (((stop_mode)::text = ANY ((ARRAY['LOW_OF_DAY'::character varying, 'OPENING_RANGE_LOW'::character varying])::text[])))
);

CREATE TABLE public.sw_config_audit (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    key character varying NOT NULL,
    old_value text,
    new_value text NOT NULL,
    changed_at timestamp with time zone NOT NULL,
    changed_by character varying NOT NULL,
    note text
);

ALTER TABLE public.sw_config_audit ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.sw_config_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.sw_fill (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    position_id bigint NOT NULL,
    side character varying NOT NULL,
    quantity integer NOT NULL,
    price numeric(18,4) NOT NULL,
    filled_at timestamp with time zone NOT NULL,
    journal_ref character varying,
    simulated boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_sw_fill_quantity_positive CHECK ((quantity > 0)),
    CONSTRAINT ck_sw_fill_side_known CHECK (((side)::text = ANY ((ARRAY['BUY'::character varying, 'SELL'::character varying])::text[])))
);

ALTER TABLE public.sw_fill ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.sw_fill_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.sw_market_daily (
    user_id bigint NOT NULL,
    date date NOT NULL,
    constituent_count integer DEFAULT 0 NOT NULL,
    pct_up_strong_1m numeric(7,4),
    pct_new_52w_high numeric(7,4),
    pct_above_ma_slow numeric(7,4),
    index_slug character varying,
    index_close numeric(18,2),
    index_ma_fast numeric(18,2),
    index_ma_slow numeric(18,2),
    gate character varying NOT NULL,
    exposure_level smallint DEFAULT '0'::smallint NOT NULL,
    max_open_positions smallint NOT NULL,
    max_exposure_pct numeric(5,2) NOT NULL,
    new_entries_allowed boolean NOT NULL,
    parabolic_count integer DEFAULT 0 NOT NULL,
    detail jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    drawdown_pct numeric(10,2) DEFAULT '0'::numeric NOT NULL,
    drawdown_locked boolean DEFAULT false NOT NULL,
    provisional boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_sw_market_daily_exposure_level_range CHECK (((exposure_level >= 0) AND (exposure_level <= 3))),
    CONSTRAINT ck_sw_market_daily_gate_known CHECK (((gate)::text = ANY ((ARRAY['GREEN'::character varying, 'AMBER'::character varying, 'RED'::character varying])::text[])))
);

CREATE TABLE public.sw_plan (
    id bigint NOT NULL,
    plan_id uuid NOT NULL,
    user_id bigint NOT NULL,
    as_of date NOT NULL,
    source character varying NOT NULL,
    built_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    plan_hash character varying NOT NULL,
    gate character varying NOT NULL,
    exposure_level smallint NOT NULL,
    total_risk_inr numeric(12,2) DEFAULT '0'::numeric NOT NULL,
    total_new_exposure_inr numeric(20,2) DEFAULT '0'::numeric NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_sw_plan_gate_known CHECK (((gate)::text = ANY ((ARRAY['GREEN'::character varying, 'AMBER'::character varying, 'RED'::character varying])::text[]))),
    CONSTRAINT ck_sw_plan_source_known CHECK (((source)::text = ANY ((ARRAY['EOD_PREVIEW'::character varying, 'MORNING'::character varying, 'SIGNAL'::character varying, 'INTRADAY'::character varying])::text[])))
);

ALTER TABLE public.sw_plan ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.sw_plan_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.sw_plan_line (
    id bigint NOT NULL,
    plan_id bigint NOT NULL,
    user_id bigint NOT NULL,
    kind character varying NOT NULL,
    instrument_id bigint NOT NULL,
    setup character varying,
    quantity integer DEFAULT 0 NOT NULL,
    trigger numeric(18,2),
    stop numeric(18,2),
    risk_inr numeric(12,2) DEFAULT '0'::numeric NOT NULL,
    position_value numeric(20,2) DEFAULT '0'::numeric NOT NULL,
    trail character varying,
    note text,
    state character varying DEFAULT 'PROPOSED'::character varying NOT NULL,
    client_id character varying NOT NULL,
    journal_ref character varying,
    position_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_sw_plan_line_kind_known CHECK (((kind)::text = ANY ((ARRAY['BUY_ON_TRIGGER'::character varying, 'SELL_AT_OPEN'::character varying, 'RAISE_GTT_STOP'::character varying, 'PENDING_RANGE'::character varying])::text[]))),
    CONSTRAINT ck_sw_plan_line_state_known CHECK (((state)::text = ANY ((ARRAY['PROPOSED'::character varying, 'CONFIRMED'::character varying, 'SENT'::character varying, 'FILLED'::character varying, 'REJECTED'::character varying, 'EXPIRED'::character varying, 'SKIPPED'::character varying])::text[])))
);

ALTER TABLE public.sw_plan_line ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.sw_plan_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.sw_plan_skip (
    id bigint NOT NULL,
    plan_id bigint NOT NULL,
    user_id bigint NOT NULL,
    instrument_id bigint,
    symbol character varying NOT NULL,
    reason character varying NOT NULL,
    detail text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_sw_plan_skip_reason_known CHECK (((reason)::text = ANY ((ARRAY['NOT_TRADEABLE_SETUP'::character varying, 'GATE_RED'::character varying, 'DRAWDOWN_LOCKOUT'::character varying, 'TIER_FULL'::character varying, 'SESSION_CAP'::character varying, 'EXPOSURE_FULL'::character varying, 'ALREADY_HELD'::character varying, 'LOCKED_UPPER_CIRCUIT'::character varying, 'SIZE_REFUSED'::character varying])::text[])))
);

ALTER TABLE public.sw_plan_skip ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.sw_plan_skip_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.sw_position (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    broker_account_id bigint,
    instrument_id bigint NOT NULL,
    setup character varying NOT NULL,
    entry_date date NOT NULL,
    entry_avg numeric(18,4) NOT NULL,
    quantity_entered integer NOT NULL,
    initial_stop numeric(18,2) NOT NULL,
    stop numeric(18,2) NOT NULL,
    gtt_id character varying,
    gtt_trigger numeric(18,2),
    gtt_armed_at timestamp with time zone,
    trail character varying NOT NULL,
    partial_done boolean DEFAULT false NOT NULL,
    partial_date date,
    quantity_open integer NOT NULL,
    state character varying DEFAULT 'OPEN'::character varying NOT NULL,
    closed_on date,
    exit_avg numeric(18,4),
    close_reason character varying,
    r_multiple numeric(8,2),
    pnl_inr numeric(12,2),
    simulated boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    half_risk boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_sw_position_close_reason_known CHECK (((close_reason)::text = ANY ((ARRAY['PARTIAL_INTO_STRENGTH'::character varying, 'BREAKEVEN_AFTER_PARTIAL'::character varying, 'BREAKEVEN_AT_R'::character varying, 'CLOSE_BELOW_TRAIL_MA'::character varying, 'EP_FAILED_RED_ON_DAY'::character varying, 'HARD_STOP_HIT'::character varying, 'NOTHING_TO_DO'::character varying, 'MANUAL'::character varying])::text[]))),
    CONSTRAINT ck_sw_position_initial_stop_below_entry CHECK ((initial_stop < entry_avg)),
    CONSTRAINT ck_sw_position_quantity_open_non_negative CHECK ((quantity_open >= 0)),
    CONSTRAINT ck_sw_position_quantity_open_within_entered CHECK ((quantity_open <= quantity_entered)),
    CONSTRAINT ck_sw_position_setup_known CHECK (((setup)::text = ANY ((ARRAY['FLAG'::character varying, 'EP'::character varying, 'PARABOLIC_SHORT'::character varying])::text[]))),
    CONSTRAINT ck_sw_position_state_known CHECK (((state)::text = ANY ((ARRAY['OPEN'::character varying, 'PARTIAL'::character varying, 'CLOSED'::character varying])::text[]))),
    CONSTRAINT ck_sw_position_stop_never_below_initial CHECK ((stop >= initial_stop)),
    CONSTRAINT ck_sw_position_trail_known CHECK (((trail)::text = ANY ((ARRAY['MA10'::character varying, 'MA20'::character varying])::text[])))
);

ALTER TABLE public.sw_position ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.sw_position_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.sw_scan_run (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    session_date date,
    provisional boolean DEFAULT false NOT NULL,
    status character varying DEFAULT 'QUEUED'::character varying NOT NULL,
    detail jsonb,
    error text,
    task_id character varying,
    CONSTRAINT ck_sw_scan_run_finished_after_started CHECK (((finished_at IS NULL) OR (started_at IS NULL) OR (finished_at >= started_at))),
    CONSTRAINT ck_sw_scan_run_status_known CHECK (((status)::text = ANY ((ARRAY['QUEUED'::character varying, 'RUNNING'::character varying, 'DONE'::character varying, 'FAILED'::character varying])::text[])))
);

ALTER TABLE public.sw_scan_run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.sw_scan_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.sw_session (
    user_id bigint NOT NULL,
    session_date date NOT NULL,
    mode character varying NOT NULL,
    monitor_ran boolean DEFAULT false NOT NULL,
    plan_ids jsonb,
    signals integer DEFAULT 0 NOT NULL,
    confirms integer DEFAULT 0 NOT NULL,
    fills integer DEFAULT 0 NOT NULL,
    manage_actions integer DEFAULT 0 NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    first_live_counted boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_sw_session_mode_known CHECK (((mode)::text = ANY ((ARRAY['DRY_RUN'::character varying, 'LIVE'::character varying])::text[])))
);

CREATE TABLE public.sw_setup_daily (
    user_id bigint NOT NULL,
    date date NOT NULL,
    instrument_id bigint NOT NULL,
    setup character varying NOT NULL,
    status character varying NOT NULL,
    score numeric(5,2) NOT NULL,
    close numeric(18,2),
    trigger numeric(18,2),
    stop_ref numeric(18,2),
    pivot_high numeric(18,2),
    adj_factor numeric(18,10),
    adr_pct numeric(10,2),
    prior_move_pct numeric(10,2),
    base_depth_pct numeric(10,2),
    tightness_adr numeric(10,2),
    dryup_ratio numeric(10,2),
    dist_ma_fast_pct numeric(10,2),
    dist_ma_slow_pct numeric(10,2),
    rvol numeric(10,2),
    gap_pct numeric(10,2),
    turnover_avg bigint,
    base_bars smallint,
    up_streak smallint,
    locked_upper_circuit boolean DEFAULT false NOT NULL,
    sector_slug character varying,
    listed_within_2y boolean DEFAULT false NOT NULL,
    pipeline_run_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    provisional boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_sw_setup_daily_setup_known CHECK (((setup)::text = ANY ((ARRAY['FLAG'::character varying, 'EP'::character varying, 'PARABOLIC_SHORT'::character varying])::text[]))),
    CONSTRAINT ck_sw_setup_daily_status_known CHECK (((status)::text = ANY ((ARRAY['SETTING_UP'::character varying, 'BREAKOUT_TODAY'::character varying, 'GAP_DAY'::character varying, 'RUNNING'::character varying, 'EXHAUSTION'::character varying])::text[])))
);

CREATE TABLE public.sw_signal (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    watch_id bigint,
    instrument_id bigint NOT NULL,
    setup character varying NOT NULL,
    session_date date NOT NULL,
    raised_at timestamp with time zone NOT NULL,
    state character varying NOT NULL,
    or_window_minutes smallint,
    range_high numeric(18,2),
    range_low numeric(18,2),
    low_of_day numeric(18,2),
    last_price numeric(18,2),
    entry numeric(18,2),
    stop numeric(18,2),
    plan_line_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_sw_signal_setup_known CHECK (((setup)::text = ANY ((ARRAY['FLAG'::character varying, 'EP'::character varying, 'PARABOLIC_SHORT'::character varying])::text[]))),
    CONSTRAINT ck_sw_signal_state_known CHECK (((state)::text = ANY ((ARRAY['TRIGGERED'::character varying, 'WAITING'::character varying, 'RANGE_INCOMPLETE'::character varying, 'BELOW_PIVOT'::character varying, 'LOCKED_UPPER_CIRCUIT'::character varying, 'SESSION_OVER'::character varying])::text[])))
);

ALTER TABLE public.sw_signal ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.sw_signal_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.sw_watch (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    setup character varying NOT NULL,
    source character varying NOT NULL,
    added_on date NOT NULL,
    expires_on date,
    trigger numeric(18,2),
    stop_ref numeric(18,2),
    setup_daily_date date,
    note text,
    catalyst text,
    state character varying DEFAULT 'WATCHING'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    score numeric(5,2),
    adr_pct numeric(10,2),
    focus boolean DEFAULT false NOT NULL,
    reconfirmed_on date,
    earnings_date date,
    CONSTRAINT ck_sw_watch_setup_known CHECK (((setup)::text = ANY ((ARRAY['FLAG'::character varying, 'EP'::character varying, 'PARABOLIC_SHORT'::character varying])::text[]))),
    CONSTRAINT ck_sw_watch_source_known CHECK (((source)::text = ANY ((ARRAY['DETECTOR'::character varying, 'MANUAL'::character varying])::text[]))),
    CONSTRAINT ck_sw_watch_state_known CHECK (((state)::text = ANY ((ARRAY['WATCHING'::character varying, 'TRIGGERED'::character varying, 'EXPIRED'::character varying, 'DISMISSED'::character varying])::text[])))
);

ALTER TABLE public.sw_watch ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.sw_watch_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.swing_candidates (
    id bigint NOT NULL,
    run_id bigint,
    run_at timestamp with time zone NOT NULL,
    mode text NOT NULL,
    symbol text NOT NULL,
    scanner text NOT NULL,
    setup text NOT NULL,
    entry numeric(14,2),
    stop numeric(14,2),
    risk_pct numeric(8,2),
    via_indices jsonb NOT NULL,
    universe_score numeric(10,4) NOT NULL,
    score numeric(10,4) NOT NULL,
    rank integer NOT NULL
);

ALTER TABLE public.swing_candidates ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.swing_candidates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.swing_regime_run (
    id bigint NOT NULL,
    run_at timestamp with time zone NOT NULL,
    mode text NOT NULL,
    kind text NOT NULL,
    as_of date NOT NULL,
    gate_open boolean NOT NULL,
    leading_count integer NOT NULL,
    min_leading integer NOT NULL,
    evaluated integer NOT NULL,
    universe_size integer NOT NULL,
    "leading" jsonb NOT NULL,
    CONSTRAINT ck_swing_regime_run_kind CHECK ((kind = ANY (ARRAY['nightly'::text, 'intraday'::text])))
);

ALTER TABLE public.swing_regime_run ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.swing_regime_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.symbol_alias (
    id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    old_symbol character varying NOT NULL,
    changed_on date NOT NULL
);

ALTER TABLE public.symbol_alias ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.symbol_alias_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.trading_day (
    exchange_id smallint NOT NULL,
    date date NOT NULL,
    is_trading_day boolean NOT NULL,
    holiday_name character varying,
    source character varying NOT NULL,
    CONSTRAINT ck_trading_day_trading_day_source CHECK (((source)::text = ANY ((ARRAY['weekend'::character varying, 'holiday'::character varying, 'derived'::character varying, 'bhavcopy'::character varying])::text[])))
);

CREATE TABLE public.tw_backtest_run (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    source character varying(24) NOT NULL,
    params jsonb NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    stats jsonb,
    drift jsonb,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_tw_backtest_run_finished_after_started CHECK (((finished_at IS NULL) OR (finished_at >= started_at))),
    CONSTRAINT ck_tw_backtest_run_source_known CHECK (((source)::text = ANY ((ARRAY['PLANT'::character varying, 'RESEARCH_EXPORT'::character varying])::text[])))
);

ALTER TABLE public.tw_backtest_run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tw_backtest_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.tw_breadth_daily (
    user_id bigint NOT NULL,
    date date NOT NULL,
    universe_count integer NOT NULL,
    measured_count integer NOT NULL,
    above_count integer NOT NULL,
    pct_above_dma numeric(7,4),
    gate character varying(8) NOT NULL,
    dma_bars smallint DEFAULT '200'::smallint NOT NULL,
    thin_session boolean DEFAULT false NOT NULL,
    detail jsonb,
    pipeline_run_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    provisional boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_tw_breadth_daily_above_within_measured CHECK ((above_count <= measured_count)),
    CONSTRAINT ck_tw_breadth_daily_gate_known CHECK (((gate)::text = ANY ((ARRAY['OPEN'::character varying, 'SHUT'::character varying])::text[]))),
    CONSTRAINT ck_tw_breadth_daily_measured_session_has_a_percentage CHECK ((thin_session OR (pct_above_dma IS NOT NULL))),
    CONSTRAINT ck_tw_breadth_daily_measured_within_universe CHECK ((measured_count <= universe_count)),
    CONSTRAINT ck_tw_breadth_daily_thin_session_is_shut CHECK (((NOT thin_session) OR ((gate)::text = 'SHUT'::text)))
);

CREATE TABLE public.tw_config (
    user_id bigint NOT NULL,
    sleeve_capital_inr numeric(12,2) DEFAULT '0'::numeric NOT NULL,
    max_open_positions smallint DEFAULT '10'::smallint NOT NULL,
    max_position_pct numeric(5,2) DEFAULT 12.50 NOT NULL,
    stop_pct numeric(5,2) DEFAULT 20.00 NOT NULL,
    trail_pct numeric(5,2) DEFAULT 20.00 NOT NULL,
    first_live_entries_left smallint DEFAULT '10'::smallint NOT NULL,
    dry_run_sessions smallint DEFAULT '0'::smallint NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(64),
    CONSTRAINT ck_tw_config_capital_non_negative CHECK ((sleeve_capital_inr >= (0)::numeric)),
    CONSTRAINT ck_tw_config_dry_run_sessions_non_negative CHECK ((dry_run_sessions >= 0)),
    CONSTRAINT ck_tw_config_first_live_non_negative CHECK ((first_live_entries_left >= 0)),
    CONSTRAINT ck_tw_config_max_positions_positive CHECK ((max_open_positions > 0)),
    CONSTRAINT ck_tw_config_position_pct_positive CHECK ((max_position_pct > (0)::numeric)),
    CONSTRAINT ck_tw_config_stop_pct_positive CHECK ((stop_pct > (0)::numeric)),
    CONSTRAINT ck_tw_config_trail_pct_positive CHECK ((trail_pct > (0)::numeric))
);

CREATE TABLE public.tw_config_audit (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    key character varying(64) NOT NULL,
    old_value text,
    new_value text,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    changed_by character varying(64) NOT NULL,
    note text
);

ALTER TABLE public.tw_config_audit ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tw_config_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.tw_fill (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    position_id bigint NOT NULL,
    order_id bigint,
    side character varying(8) NOT NULL,
    quantity integer NOT NULL,
    price numeric(18,4) NOT NULL,
    filled_at timestamp with time zone NOT NULL,
    journal_ref character varying(64),
    simulated boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_tw_fill_price_positive CHECK ((price > (0)::numeric)),
    CONSTRAINT ck_tw_fill_quantity_positive CHECK ((quantity > 0)),
    CONSTRAINT ck_tw_fill_side_known CHECK (((side)::text = ANY ((ARRAY['BUY'::character varying, 'SELL'::character varying])::text[])))
);

ALTER TABLE public.tw_fill ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tw_fill_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.tw_order (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    broker_account_id bigint,
    instrument_id bigint NOT NULL,
    signal_date date NOT NULL,
    side character varying(8) NOT NULL,
    quantity integer NOT NULL,
    stop_price numeric(18,2) NOT NULL,
    state character varying(16) DEFAULT 'PROPOSED'::character varying NOT NULL,
    broker_order_id character varying(64),
    client_id character varying(128),
    filled_quantity integer DEFAULT 0 NOT NULL,
    avg_fill_price numeric(18,4),
    position_id bigint,
    simulated boolean DEFAULT true NOT NULL,
    detail jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_tw_order_filled_non_negative CHECK ((filled_quantity >= 0)),
    CONSTRAINT ck_tw_order_filled_within_quantity CHECK ((filled_quantity <= quantity)),
    CONSTRAINT ck_tw_order_quantity_positive CHECK ((quantity > 0)),
    CONSTRAINT ck_tw_order_side_known CHECK (((side)::text = ANY ((ARRAY['BUY'::character varying, 'SELL'::character varying])::text[]))),
    CONSTRAINT ck_tw_order_state_known CHECK (((state)::text = ANY ((ARRAY['PROPOSED'::character varying, 'CONFIRMED'::character varying, 'SENT'::character varying, 'FILLED'::character varying, 'PARTIAL'::character varying, 'CANCELLED'::character varying, 'REJECTED'::character varying])::text[]))),
    CONSTRAINT ck_tw_order_stop_positive CHECK ((stop_price > (0)::numeric))
);

ALTER TABLE public.tw_order ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tw_order_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.tw_plan (
    id bigint NOT NULL,
    plan_id uuid NOT NULL,
    user_id bigint NOT NULL,
    session_date date NOT NULL,
    source character varying(16) NOT NULL,
    built_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    plan_hash character varying(64) NOT NULL,
    gate character varying(8) NOT NULL,
    sleeve_equity_inr numeric(12,2) NOT NULL,
    total_new_exposure_inr numeric(12,2) DEFAULT '0'::numeric NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_tw_plan_equity_non_negative CHECK ((sleeve_equity_inr >= (0)::numeric)),
    CONSTRAINT ck_tw_plan_expiry_after_build CHECK ((expires_at > built_at)),
    CONSTRAINT ck_tw_plan_exposure_non_negative CHECK ((total_new_exposure_inr >= (0)::numeric)),
    CONSTRAINT ck_tw_plan_gate_known CHECK (((gate)::text = ANY ((ARRAY['OPEN'::character varying, 'SHUT'::character varying])::text[]))),
    CONSTRAINT ck_tw_plan_source_known CHECK (((source)::text = ANY ((ARRAY['EVENING'::character varying, 'MORNING'::character varying, 'MANUAL'::character varying, 'LIVE'::character varying])::text[])))
);

ALTER TABLE public.tw_plan ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tw_plan_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.tw_plan_line (
    id bigint NOT NULL,
    plan_id bigint NOT NULL,
    user_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    kind character varying(16) NOT NULL,
    state character varying(16) DEFAULT 'PROPOSED'::character varying NOT NULL,
    quantity integer DEFAULT 0 NOT NULL,
    stop_price numeric(18,2),
    value_inr numeric(12,2) DEFAULT '0'::numeric NOT NULL,
    high_since numeric(18,4),
    previous_trigger numeric(18,2),
    note text,
    client_id character varying(128),
    journal_ref character varying(64),
    order_id bigint,
    position_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_tw_plan_line_kind_known CHECK (((kind)::text = ANY ((ARRAY['BUY_AT_OPEN'::character varying, 'ARM_GTT'::character varying, 'RAISE_GTT_STOP'::character varying, 'SELL_AT_OPEN'::character varying])::text[]))),
    CONSTRAINT ck_tw_plan_line_quantity_non_negative CHECK ((quantity >= 0)),
    CONSTRAINT ck_tw_plan_line_state_known CHECK (((state)::text = ANY ((ARRAY['PROPOSED'::character varying, 'CONFIRMED'::character varying, 'SENT'::character varying, 'FILLED'::character varying, 'REJECTED'::character varying, 'EXPIRED'::character varying, 'SKIPPED'::character varying])::text[]))),
    CONSTRAINT ck_tw_plan_line_value_non_negative CHECK ((value_inr >= (0)::numeric))
);

ALTER TABLE public.tw_plan_line ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tw_plan_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.tw_plan_skip (
    id bigint NOT NULL,
    plan_id bigint NOT NULL,
    user_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    reason character varying(32) NOT NULL,
    detail text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_tw_plan_skip_reason_known CHECK (((reason)::text = ANY ((ARRAY['GATE_SHUT'::character varying, 'ALREADY_HELD'::character varying, 'SESSION_CAP'::character varying, 'SLOTS_FULL'::character varying, 'EXPOSURE_FULL'::character varying, 'BELOW_MIN_TRADE_VALUE'::character varying, 'TURNOVER_CAP'::character varying, 'LOCKED_UPPER_CIRCUIT'::character varying, 'NO_SLEEVE_CAPITAL'::character varying, 'NO_BAR'::character varying, 'BELOW_LIQUIDITY_FLOOR'::character varying, 'STOP_NOT_BELOW_ENTRY'::character varying])::text[])))
);

ALTER TABLE public.tw_plan_skip ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tw_plan_skip_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.tw_position (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    broker_account_id bigint,
    instrument_id bigint NOT NULL,
    order_id bigint,
    signal_date date NOT NULL,
    entry_date date NOT NULL,
    entry_avg numeric(18,4) NOT NULL,
    quantity_entered integer NOT NULL,
    entry_adj_factor numeric(18,10) DEFAULT '1'::numeric NOT NULL,
    initial_stop numeric(18,2) NOT NULL,
    stop_price numeric(18,2) NOT NULL,
    high_since numeric(18,4) NOT NULL,
    high_since_date date,
    gtt_id character varying(64),
    gtt_trigger numeric(18,2),
    gtt_armed_at timestamp with time zone,
    next_trigger numeric(18,2),
    next_trigger_for date,
    quantity_open integer NOT NULL,
    state character varying(16) DEFAULT 'OPEN'::character varying NOT NULL,
    closed_on date,
    exit_avg numeric(18,4),
    close_reason character varying(24),
    pnl_inr numeric(12,2),
    return_pct numeric(10,2),
    r_multiple numeric(10,2),
    hold_sessions smallint,
    simulated boolean DEFAULT true NOT NULL,
    half_size boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    partial_done boolean DEFAULT false NOT NULL,
    trail character varying(4),
    partial_queued_for date,
    partial_quantity integer,
    exit_queued_for date,
    exit_reason_queued character varying(24),
    CONSTRAINT ck_tw_position_close_reason_known CHECK (((close_reason)::text = ANY ((ARRAY['STOP_HIT'::character varying, 'STOP_GAP'::character varying, 'STOP_DAY0'::character varying, 'NO_BAR'::character varying, 'MANUAL'::character varying, 'MA_TRAIL'::character varying])::text[]))),
    CONSTRAINT ck_tw_position_entry_and_stop_positive CHECK (((entry_avg > (0)::numeric) AND (initial_stop > (0)::numeric))),
    CONSTRAINT ck_tw_position_high_since_positive CHECK ((high_since > (0)::numeric)),
    CONSTRAINT ck_tw_position_initial_stop_below_entry CHECK ((initial_stop < entry_avg)),
    CONSTRAINT ck_tw_position_next_trigger_has_a_session CHECK (((next_trigger IS NULL) OR (next_trigger_for IS NOT NULL))),
    CONSTRAINT ck_tw_position_open_non_negative CHECK ((quantity_open >= 0)),
    CONSTRAINT ck_tw_position_open_within_entered CHECK ((quantity_open <= quantity_entered)),
    CONSTRAINT ck_tw_position_quantity_positive CHECK ((quantity_entered > 0)),
    CONSTRAINT ck_tw_position_state_known CHECK (((state)::text = ANY ((ARRAY['OPEN'::character varying, 'CLOSED'::character varying])::text[]))),
    CONSTRAINT ck_tw_position_stop_never_below_initial CHECK ((stop_price >= initial_stop))
);

ALTER TABLE public.tw_position ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tw_position_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.tw_scan_run (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    session_date date,
    status character varying(16) DEFAULT 'QUEUED'::character varying NOT NULL,
    source character varying(8) DEFAULT 'desk'::character varying NOT NULL,
    detail jsonb,
    error text,
    task_id character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    provisional boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_tw_scan_run_finished_after_started CHECK (((finished_at IS NULL) OR (started_at IS NULL) OR (finished_at >= started_at))),
    CONSTRAINT ck_tw_scan_run_source_known CHECK (((source)::text = ANY ((ARRAY['desk'::character varying, 'web'::character varying, 'cli'::character varying])::text[]))),
    CONSTRAINT ck_tw_scan_run_status_known CHECK (((status)::text = ANY ((ARRAY['QUEUED'::character varying, 'RUNNING'::character varying, 'DONE'::character varying, 'FAILED'::character varying])::text[])))
);

ALTER TABLE public.tw_scan_run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tw_scan_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.tw_session (
    user_id bigint NOT NULL,
    session_date date NOT NULL,
    mode character varying(8) DEFAULT 'DRY_RUN'::character varying NOT NULL,
    gate character varying(8) DEFAULT 'SHUT'::character varying NOT NULL,
    states integer DEFAULT 0 NOT NULL,
    signals integer DEFAULT 0 NOT NULL,
    orders_placed integer DEFAULT 0 NOT NULL,
    confirms integer DEFAULT 0 NOT NULL,
    fills integer DEFAULT 0 NOT NULL,
    ratchets integer DEFAULT 0 NOT NULL,
    exits integer DEFAULT 0 NOT NULL,
    naked_at_1515 integer DEFAULT 0 NOT NULL,
    first_live_entries_counted smallint DEFAULT '0'::smallint NOT NULL,
    counted_for_dry_run boolean DEFAULT false NOT NULL,
    plan_ids jsonb,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_tw_session_counters_non_negative CHECK (((states >= 0) AND (signals >= 0) AND (orders_placed >= 0) AND (confirms >= 0) AND (fills >= 0) AND (ratchets >= 0) AND (exits >= 0) AND (naked_at_1515 >= 0) AND (first_live_entries_counted >= 0))),
    CONSTRAINT ck_tw_session_gate_known CHECK (((gate)::text = ANY ((ARRAY['OPEN'::character varying, 'SHUT'::character varying])::text[]))),
    CONSTRAINT ck_tw_session_mode_known CHECK (((mode)::text = ANY ((ARRAY['DRY_RUN'::character varying, 'LIVE'::character varying])::text[])))
);

CREATE TABLE public.tw_signal_daily (
    user_id bigint NOT NULL,
    date date NOT NULL,
    instrument_id bigint NOT NULL,
    state character varying(16) NOT NULL,
    failed_filters character varying(16)[] DEFAULT '{}'::character varying[] NOT NULL,
    entry_reference_close numeric(18,2) NOT NULL,
    stop_preview numeric(18,2) NOT NULL,
    sessions_out_before smallint NOT NULL,
    rank_key bigint DEFAULT '0'::bigint NOT NULL,
    turnover_avg_20 bigint,
    pipeline_run_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    provisional boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_tw_signal_daily_entry_reference_positive CHECK ((entry_reference_close > (0)::numeric)),
    CONSTRAINT ck_tw_signal_daily_sessions_out_non_negative CHECK ((sessions_out_before >= 0)),
    CONSTRAINT ck_tw_signal_daily_state_known CHECK (((state)::text = ANY ((ARRAY['SIGNAL'::character varying, 'SCAN_ONLY'::character varying])::text[]))),
    CONSTRAINT ck_tw_signal_daily_stop_below_entry CHECK (((stop_preview > (0)::numeric) AND (stop_preview < entry_reference_close)))
);

CREATE TABLE public.tw_state_daily (
    user_id bigint NOT NULL,
    date date NOT NULL,
    instrument_id bigint NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    close_raw numeric(18,4) NOT NULL,
    adj_factor numeric(18,10) DEFAULT '1'::numeric NOT NULL,
    week_close_0 numeric(18,2) NOT NULL,
    week_close_1 numeric(18,2) NOT NULL,
    week_close_2 numeric(18,2) NOT NULL,
    week_range_pct numeric(10,4) NOT NULL,
    month_low_3 numeric(18,2) NOT NULL,
    month_low_ratio numeric(10,4) NOT NULL,
    vol_sma_50 bigint NOT NULL,
    volume bigint NOT NULL,
    turnover_inr bigint NOT NULL,
    turnover_avg_20 bigint,
    sma_dma numeric(18,2),
    sessions_in_state smallint NOT NULL,
    bars_in_window smallint,
    locked_upper_circuit boolean DEFAULT false NOT NULL,
    pipeline_run_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    provisional boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_tw_state_daily_bars_non_negative CHECK (((bars_in_window IS NULL) OR (bars_in_window >= 0))),
    CONSTRAINT ck_tw_state_daily_close_positive CHECK (((close > (0)::numeric) AND (close_raw > (0)::numeric))),
    CONSTRAINT ck_tw_state_daily_month_low_positive CHECK (((month_low_3 > (0)::numeric) AND (month_low_ratio > (0)::numeric))),
    CONSTRAINT ck_tw_state_daily_sessions_in_state_positive CHECK ((sessions_in_state > 0)),
    CONSTRAINT ck_tw_state_daily_volume_non_negative CHECK (((volume >= 0) AND (vol_sma_50 >= 0))),
    CONSTRAINT ck_tw_state_daily_week_range_non_negative CHECK ((week_range_pct >= (0)::numeric))
);

CREATE TABLE public.user_discover_preferences (
    user_id bigint NOT NULL,
    goal character varying NOT NULL,
    horizon character varying NOT NULL,
    risk character varying NOT NULL,
    amount numeric(12,2) NOT NULL,
    rebalance character varying NOT NULL,
    onboarding_completed_at timestamp with time zone,
    updated_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.vb_backtest_run (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    source character varying(24) NOT NULL,
    params jsonb NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    stats jsonb,
    drift jsonb,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_vb_backtest_run_source_known CHECK (((source)::text = ANY ((ARRAY['PLANT'::character varying, 'RESEARCH_EXPORT'::character varying])::text[])))
);

ALTER TABLE public.vb_backtest_run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.vb_backtest_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.vb_breadth_daily (
    user_id bigint NOT NULL,
    date date NOT NULL,
    universe_count integer NOT NULL,
    measured_count integer NOT NULL,
    above_count integer NOT NULL,
    pct_above_dma numeric(7,4) NOT NULL,
    gate character varying(8) NOT NULL,
    dma_bars smallint DEFAULT '200'::smallint NOT NULL,
    thin_session boolean DEFAULT false NOT NULL,
    detail jsonb,
    pipeline_run_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    provisional boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_vb_breadth_daily_above_within_measured CHECK ((above_count <= measured_count)),
    CONSTRAINT ck_vb_breadth_daily_gate_known CHECK (((gate)::text = ANY ((ARRAY['OPEN'::character varying, 'SHUT'::character varying])::text[]))),
    CONSTRAINT ck_vb_breadth_daily_measured_within_universe CHECK ((measured_count <= universe_count))
);

CREATE TABLE public.vb_config (
    user_id bigint NOT NULL,
    sleeve_capital_inr numeric(20,2) DEFAULT '0'::numeric NOT NULL,
    max_open_positions smallint DEFAULT '10'::smallint NOT NULL,
    max_position_pct numeric(5,2) DEFAULT 12.50 NOT NULL,
    stop_pct numeric(5,2) DEFAULT 12.00 NOT NULL,
    first_live_sessions_left smallint DEFAULT '5'::smallint NOT NULL,
    dry_run_sessions integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(64),
    CONSTRAINT ck_vb_config_capital_non_negative CHECK ((sleeve_capital_inr >= (0)::numeric)),
    CONSTRAINT ck_vb_config_dry_run_sessions_non_negative CHECK ((dry_run_sessions >= 0)),
    CONSTRAINT ck_vb_config_first_live_non_negative CHECK ((first_live_sessions_left >= 0)),
    CONSTRAINT ck_vb_config_max_positions_positive CHECK ((max_open_positions > 0)),
    CONSTRAINT ck_vb_config_position_pct_positive CHECK ((max_position_pct > (0)::numeric)),
    CONSTRAINT ck_vb_config_stop_pct_positive CHECK ((stop_pct > (0)::numeric))
);

CREATE TABLE public.vb_config_audit (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    key character varying(64) NOT NULL,
    old_value text,
    new_value text,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    changed_by character varying(64) NOT NULL,
    note text
);

ALTER TABLE public.vb_config_audit ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.vb_config_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.vb_fill (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    position_id bigint NOT NULL,
    order_id bigint,
    side character varying(8) NOT NULL,
    quantity integer NOT NULL,
    price numeric(18,4) NOT NULL,
    filled_at timestamp with time zone NOT NULL,
    journal_ref character varying(64),
    simulated boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_vb_fill_price_positive CHECK ((price > (0)::numeric)),
    CONSTRAINT ck_vb_fill_quantity_positive CHECK ((quantity > 0)),
    CONSTRAINT ck_vb_fill_side_known CHECK (((side)::text = ANY ((ARRAY['BUY'::character varying, 'SELL'::character varying])::text[])))
);

ALTER TABLE public.vb_fill ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.vb_fill_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.vb_order (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    broker_account_id bigint,
    instrument_id bigint NOT NULL,
    signal_date date NOT NULL,
    limit_price numeric(18,2) NOT NULL,
    stop_price numeric(18,2) NOT NULL,
    quantity integer NOT NULL,
    state character varying(16) NOT NULL,
    working_from date,
    expires_after_session date,
    sessions_worked smallint DEFAULT '0'::smallint NOT NULL,
    broker_order_id character varying(64),
    client_id character varying(128),
    filled_quantity integer DEFAULT 0 NOT NULL,
    avg_fill_price numeric(18,4),
    position_id bigint,
    cancelled_on date,
    cancel_reason character varying(16),
    simulated boolean DEFAULT true NOT NULL,
    detail jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_vb_order_cancel_reason_known CHECK (((cancel_reason)::text = ANY ((ARRAY['EXPIRY_SWEEP'::character varying, 'MANUAL'::character varying, 'GATE_SHUT'::character varying, 'SLOT_TAKEN'::character varying])::text[]))),
    CONSTRAINT ck_vb_order_filled_non_negative CHECK ((filled_quantity >= 0)),
    CONSTRAINT ck_vb_order_filled_within_quantity CHECK ((filled_quantity <= quantity)),
    CONSTRAINT ck_vb_order_limit_positive CHECK ((limit_price > (0)::numeric)),
    CONSTRAINT ck_vb_order_quantity_positive CHECK ((quantity > 0)),
    CONSTRAINT ck_vb_order_sessions_worked_non_negative CHECK ((sessions_worked >= 0)),
    CONSTRAINT ck_vb_order_state_known CHECK (((state)::text = ANY ((ARRAY['PROPOSED'::character varying, 'CONFIRMED'::character varying, 'SENT'::character varying, 'PARTIAL'::character varying, 'FILLED'::character varying, 'CANCELLED'::character varying, 'EXPIRED'::character varying, 'REJECTED'::character varying])::text[]))),
    CONSTRAINT ck_vb_order_stop_below_limit CHECK (((stop_price > (0)::numeric) AND (stop_price < limit_price)))
);

ALTER TABLE public.vb_order ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.vb_order_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.vb_plan (
    id bigint NOT NULL,
    plan_id uuid NOT NULL,
    user_id bigint NOT NULL,
    session_date date NOT NULL,
    source character varying(16) NOT NULL,
    built_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    plan_hash character varying(64) NOT NULL,
    gate character varying(8) NOT NULL,
    sleeve_equity_inr numeric(20,2) NOT NULL,
    total_new_exposure_inr numeric(20,2) DEFAULT '0'::numeric NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_vb_plan_gate_known CHECK (((gate)::text = ANY ((ARRAY['OPEN'::character varying, 'SHUT'::character varying])::text[]))),
    CONSTRAINT ck_vb_plan_source_known CHECK (((source)::text = ANY ((ARRAY['EVENING'::character varying, 'MORNING'::character varying, 'MANUAL'::character varying, 'LIVE'::character varying])::text[])))
);

ALTER TABLE public.vb_plan ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.vb_plan_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.vb_plan_line (
    id bigint NOT NULL,
    plan_id bigint NOT NULL,
    user_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    kind character varying(16) NOT NULL,
    state character varying(16) DEFAULT 'PROPOSED'::character varying NOT NULL,
    quantity integer DEFAULT 0 NOT NULL,
    limit_price numeric(18,2),
    stop_price numeric(18,2),
    value_inr numeric(20,2) DEFAULT '0'::numeric NOT NULL,
    size_cap character varying(16),
    reason character varying(24),
    note text,
    client_id character varying(128),
    journal_ref character varying(64),
    order_id bigint,
    position_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_vb_plan_line_kind_known CHECK (((kind)::text = ANY ((ARRAY['PLACE_LIMIT'::character varying, 'SELL_AT_OPEN'::character varying, 'CANCEL_LIMIT'::character varying, 'ARM_GTT'::character varying, 'BUY_AT_MARKET'::character varying, 'RAISE_GTT_STOP'::character varying])::text[]))),
    CONSTRAINT ck_vb_plan_line_quantity_non_negative CHECK ((quantity >= 0)),
    CONSTRAINT ck_vb_plan_line_state_known CHECK (((state)::text = ANY ((ARRAY['PROPOSED'::character varying, 'CONFIRMED'::character varying, 'SENT'::character varying, 'FILLED'::character varying, 'REJECTED'::character varying, 'EXPIRED'::character varying, 'SKIPPED'::character varying])::text[])))
);

ALTER TABLE public.vb_plan_line ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.vb_plan_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.vb_plan_skip (
    id bigint NOT NULL,
    plan_id bigint NOT NULL,
    user_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    reason character varying(32) NOT NULL,
    detail text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_vb_plan_skip_reason_known CHECK (((reason)::text = ANY ((ARRAY['NO_SLEEVE_CAPITAL'::character varying, 'GATE_SHUT'::character varying, 'LOCKED_UPPER_CIRCUIT'::character varying, 'ALREADY_HELD'::character varying, 'ALREADY_WORKING'::character varying, 'SESSION_CAP'::character varying, 'SLOTS_FULL'::character varying, 'STOP_NOT_BELOW_ENTRY'::character varying, 'BELOW_MIN_TRADE_VALUE'::character varying, 'TURNOVER_CAP'::character varying, 'EXPOSURE_FULL'::character varying])::text[])))
);

ALTER TABLE public.vb_plan_skip ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.vb_plan_skip_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.vb_position (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    broker_account_id bigint,
    instrument_id bigint NOT NULL,
    entry_date date NOT NULL,
    entry_avg numeric(18,4) NOT NULL,
    quantity_entered integer NOT NULL,
    quantity_open integer NOT NULL,
    initial_stop numeric(18,2) NOT NULL,
    stop_price numeric(18,2) NOT NULL,
    gtt_id character varying(64),
    gtt_trigger numeric(18,2),
    gtt_armed_at timestamp with time zone,
    state character varying(16) DEFAULT 'OPEN'::character varying NOT NULL,
    exit_queued_for date,
    exit_reason_queued character varying(24),
    closed_on date,
    exit_avg numeric(18,4),
    close_reason character varying(24),
    pnl_inr numeric(12,2),
    return_pct numeric(10,2),
    r_multiple numeric(10,2),
    hold_sessions smallint,
    size_cap character varying(16),
    simulated boolean DEFAULT true NOT NULL,
    half_risk boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    partial_done boolean DEFAULT false NOT NULL,
    trail character varying(4),
    CONSTRAINT ck_vb_position_close_reason_known CHECK (((close_reason)::text = ANY ((ARRAY['EMA_EXIT'::character varying, 'STOP_HIT'::character varying, 'STOP_GAP'::character varying, 'NO_BAR'::character varying, 'END_OF_RUN'::character varying, 'MANUAL'::character varying, 'MA_TRAIL'::character varying, 'PARTIAL'::character varying])::text[]))),
    CONSTRAINT ck_vb_position_open_non_negative CHECK ((quantity_open >= 0)),
    CONSTRAINT ck_vb_position_open_within_entered CHECK ((quantity_open <= quantity_entered)),
    CONSTRAINT ck_vb_position_quantity_positive CHECK ((quantity_entered > 0)),
    CONSTRAINT ck_vb_position_state_known CHECK (((state)::text = ANY ((ARRAY['OPEN'::character varying, 'CLOSED'::character varying])::text[]))),
    CONSTRAINT ck_vb_position_stop_never_below_initial CHECK ((stop_price >= initial_stop))
);

ALTER TABLE public.vb_position ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.vb_position_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.vb_scan_run (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    session_date date,
    status character varying(16) DEFAULT 'QUEUED'::character varying NOT NULL,
    source character varying(8) DEFAULT 'desk'::character varying NOT NULL,
    detail jsonb,
    error text,
    task_id character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    provisional boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_vb_scan_run_finished_after_started CHECK (((finished_at IS NULL) OR (started_at IS NULL) OR (finished_at >= started_at))),
    CONSTRAINT ck_vb_scan_run_source_known CHECK (((source)::text = ANY ((ARRAY['desk'::character varying, 'cli'::character varying])::text[]))),
    CONSTRAINT ck_vb_scan_run_status_known CHECK (((status)::text = ANY ((ARRAY['QUEUED'::character varying, 'RUNNING'::character varying, 'DONE'::character varying, 'FAILED'::character varying])::text[])))
);

ALTER TABLE public.vb_scan_run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.vb_scan_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.vb_session (
    user_id bigint NOT NULL,
    session_date date NOT NULL,
    mode character varying(8) DEFAULT 'DRY_RUN'::character varying NOT NULL,
    gate character varying(8) DEFAULT 'SHUT'::character varying NOT NULL,
    signals integer DEFAULT 0 NOT NULL,
    orders_placed integer DEFAULT 0 NOT NULL,
    orders_expired integer DEFAULT 0 NOT NULL,
    confirms integer DEFAULT 0 NOT NULL,
    fills integer DEFAULT 0 NOT NULL,
    exits integer DEFAULT 0 NOT NULL,
    first_live_counted boolean DEFAULT false NOT NULL,
    counted_for_dry_run_gate boolean DEFAULT false NOT NULL,
    plan_ids jsonb,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_vb_session_gate_known CHECK (((gate)::text = ANY ((ARRAY['OPEN'::character varying, 'SHUT'::character varying])::text[]))),
    CONSTRAINT ck_vb_session_mode_known CHECK (((mode)::text = ANY ((ARRAY['DRY_RUN'::character varying, 'LIVE'::character varying])::text[])))
);

CREATE TABLE public.vb_signal_daily (
    user_id bigint NOT NULL,
    date date NOT NULL,
    instrument_id bigint NOT NULL,
    state character varying(16) NOT NULL,
    failed_filters character varying(1)[] DEFAULT '{}'::character varying[] NOT NULL,
    open numeric(18,2),
    high numeric(18,2),
    low numeric(18,2),
    close numeric(18,2) NOT NULL,
    close_raw numeric(18,4) NOT NULL,
    adj_factor numeric(18,10) DEFAULT '1'::numeric NOT NULL,
    limit_price numeric(18,2) NOT NULL,
    stop_price numeric(18,2) NOT NULL,
    change_pct numeric(10,2),
    rvol numeric(10,2),
    close_position numeric(6,4),
    ret_20_pct numeric(10,2),
    volume bigint,
    vol_sma_50 bigint,
    turnover_inr bigint,
    turnover_avg_20 bigint,
    sma_200 numeric(18,2),
    ema_21 numeric(18,2),
    high_20_prior numeric(18,2),
    rank_key bigint DEFAULT '0'::bigint NOT NULL,
    locked_upper_circuit boolean DEFAULT false NOT NULL,
    bars_in_window smallint,
    pipeline_run_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    provisional boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_vb_signal_daily_limit_positive CHECK ((limit_price > (0)::numeric)),
    CONSTRAINT ck_vb_signal_daily_state_known CHECK (((state)::text = ANY ((ARRAY['SIGNAL'::character varying, 'SCAN_ONLY'::character varying])::text[]))),
    CONSTRAINT ck_vb_signal_daily_stop_below_limit CHECK (((stop_price > (0)::numeric) AND (stop_price < limit_price)))
);

CREATE TABLE public.webhook_delivery (
    id bigint NOT NULL,
    endpoint_id bigint NOT NULL,
    event character varying NOT NULL,
    idempotency_key character varying NOT NULL,
    payload jsonb NOT NULL,
    status character varying DEFAULT 'pending'::character varying NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone,
    response_status integer,
    last_error character varying,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    delivered_at timestamp with time zone,
    CONSTRAINT ck_webhook_delivery_ck_webhook_delivery_webhook_delivery_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'delivered'::character varying, 'failed'::character varying])::text[])))
);

ALTER TABLE public.webhook_delivery ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.webhook_delivery_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.webhook_endpoint (
    id bigint NOT NULL,
    public_id character varying NOT NULL,
    user_id bigint NOT NULL,
    screen_id bigint NOT NULL,
    url character varying NOT NULL,
    secret_version integer DEFAULT 1 NOT NULL,
    events jsonb DEFAULT '[]'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    disabled_at timestamp with time zone,
    disabled_reason character varying,
    last_delivery_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.webhook_endpoint ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.webhook_endpoint_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.webhook_event (
    id bigint NOT NULL,
    provider character varying NOT NULL,
    event_id character varying NOT NULL,
    event_type character varying NOT NULL,
    payload jsonb NOT NULL,
    status character varying NOT NULL,
    note character varying,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    processed_at timestamp with time zone,
    CONSTRAINT ck_webhook_event_webhook_event_status CHECK (((status)::text = ANY ((ARRAY['received'::character varying, 'processed'::character varying, 'ignored'::character varying, 'failed'::character varying])::text[])))
);

CREATE SEQUENCE public.webhook_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.webhook_event_id_seq OWNED BY public.webhook_event.id;

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202201 FOR VALUES FROM ('2022-01-01') TO ('2022-02-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202202 FOR VALUES FROM ('2022-02-01') TO ('2022-03-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202203 FOR VALUES FROM ('2022-03-01') TO ('2022-04-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202204 FOR VALUES FROM ('2022-04-01') TO ('2022-05-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202205 FOR VALUES FROM ('2022-05-01') TO ('2022-06-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202206 FOR VALUES FROM ('2022-06-01') TO ('2022-07-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202207 FOR VALUES FROM ('2022-07-01') TO ('2022-08-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202208 FOR VALUES FROM ('2022-08-01') TO ('2022-09-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202209 FOR VALUES FROM ('2022-09-01') TO ('2022-10-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202210 FOR VALUES FROM ('2022-10-01') TO ('2022-11-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202211 FOR VALUES FROM ('2022-11-01') TO ('2022-12-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202212 FOR VALUES FROM ('2022-12-01') TO ('2023-01-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202301 FOR VALUES FROM ('2023-01-01') TO ('2023-02-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202302 FOR VALUES FROM ('2023-02-01') TO ('2023-03-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202303 FOR VALUES FROM ('2023-03-01') TO ('2023-04-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202304 FOR VALUES FROM ('2023-04-01') TO ('2023-05-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202305 FOR VALUES FROM ('2023-05-01') TO ('2023-06-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202306 FOR VALUES FROM ('2023-06-01') TO ('2023-07-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202307 FOR VALUES FROM ('2023-07-01') TO ('2023-08-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202308 FOR VALUES FROM ('2023-08-01') TO ('2023-09-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202309 FOR VALUES FROM ('2023-09-01') TO ('2023-10-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202310 FOR VALUES FROM ('2023-10-01') TO ('2023-11-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202311 FOR VALUES FROM ('2023-11-01') TO ('2023-12-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202312 FOR VALUES FROM ('2023-12-01') TO ('2024-01-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202401 FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202402 FOR VALUES FROM ('2024-02-01') TO ('2024-03-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202403 FOR VALUES FROM ('2024-03-01') TO ('2024-04-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202404 FOR VALUES FROM ('2024-04-01') TO ('2024-05-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202405 FOR VALUES FROM ('2024-05-01') TO ('2024-06-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202406 FOR VALUES FROM ('2024-06-01') TO ('2024-07-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202407 FOR VALUES FROM ('2024-07-01') TO ('2024-08-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202408 FOR VALUES FROM ('2024-08-01') TO ('2024-09-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202409 FOR VALUES FROM ('2024-09-01') TO ('2024-10-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202410 FOR VALUES FROM ('2024-10-01') TO ('2024-11-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202411 FOR VALUES FROM ('2024-11-01') TO ('2024-12-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202412 FOR VALUES FROM ('2024-12-01') TO ('2025-01-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202501 FOR VALUES FROM ('2025-01-01') TO ('2025-02-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202502 FOR VALUES FROM ('2025-02-01') TO ('2025-03-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202503 FOR VALUES FROM ('2025-03-01') TO ('2025-04-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202504 FOR VALUES FROM ('2025-04-01') TO ('2025-05-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202505 FOR VALUES FROM ('2025-05-01') TO ('2025-06-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202506 FOR VALUES FROM ('2025-06-01') TO ('2025-07-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202507 FOR VALUES FROM ('2025-07-01') TO ('2025-08-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202508 FOR VALUES FROM ('2025-08-01') TO ('2025-09-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202509 FOR VALUES FROM ('2025-09-01') TO ('2025-10-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202510 FOR VALUES FROM ('2025-10-01') TO ('2025-11-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202511 FOR VALUES FROM ('2025-11-01') TO ('2025-12-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202512 FOR VALUES FROM ('2025-12-01') TO ('2026-01-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202601 FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202602 FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202603 FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202604 FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202605 FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202606 FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202607 FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202608 FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202609 FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202610 FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202611 FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202612 FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202701 FOR VALUES FROM ('2027-01-01') TO ('2027-02-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202702 FOR VALUES FROM ('2027-02-01') TO ('2027-03-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202703 FOR VALUES FROM ('2027-03-01') TO ('2027-04-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202704 FOR VALUES FROM ('2027-04-01') TO ('2027-05-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202705 FOR VALUES FROM ('2027-05-01') TO ('2027-06-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202706 FOR VALUES FROM ('2027-06-01') TO ('2027-07-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202707 FOR VALUES FROM ('2027-07-01') TO ('2027-08-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202708 FOR VALUES FROM ('2027-08-01') TO ('2027-09-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202709 FOR VALUES FROM ('2027-09-01') TO ('2027-10-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202710 FOR VALUES FROM ('2027-10-01') TO ('2027-11-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202711 FOR VALUES FROM ('2027-11-01') TO ('2027-12-01');

ALTER TABLE ONLY public.fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202712 FOR VALUES FROM ('2027-12-01') TO ('2028-01-01');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202609 FOR VALUES FROM ('2026-08-31 18:30:00+00') TO ('2026-09-30 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202610 FOR VALUES FROM ('2026-09-30 18:30:00+00') TO ('2026-10-31 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202611 FOR VALUES FROM ('2026-10-31 18:30:00+00') TO ('2026-11-30 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202612 FOR VALUES FROM ('2026-11-30 18:30:00+00') TO ('2026-12-31 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202701 FOR VALUES FROM ('2026-12-31 18:30:00+00') TO ('2027-01-31 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202702 FOR VALUES FROM ('2027-01-31 18:30:00+00') TO ('2027-02-28 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202703 FOR VALUES FROM ('2027-02-28 18:30:00+00') TO ('2027-03-31 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202704 FOR VALUES FROM ('2027-03-31 18:30:00+00') TO ('2027-04-30 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202705 FOR VALUES FROM ('2027-04-30 18:30:00+00') TO ('2027-05-31 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202706 FOR VALUES FROM ('2027-05-31 18:30:00+00') TO ('2027-06-30 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202707 FOR VALUES FROM ('2027-06-30 18:30:00+00') TO ('2027-07-31 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202708 FOR VALUES FROM ('2027-07-31 18:30:00+00') TO ('2027-08-31 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202709 FOR VALUES FROM ('2027-08-31 18:30:00+00') TO ('2027-09-30 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202710 FOR VALUES FROM ('2027-09-30 18:30:00+00') TO ('2027-10-31 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202711 FOR VALUES FROM ('2027-10-31 18:30:00+00') TO ('2027-11-30 18:30:00+00');

ALTER TABLE ONLY public.op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202712 FOR VALUES FROM ('2027-11-30 18:30:00+00') TO ('2027-12-31 18:30:00+00');

ALTER TABLE ONLY public.auth_identity ALTER COLUMN id SET DEFAULT nextval('public.auth_identity_id_seq'::regclass);

ALTER TABLE ONLY public.basket_snapshot ALTER COLUMN id SET DEFAULT nextval('public.basket_snapshot_id_seq'::regclass);

ALTER TABLE ONLY public.cb_manager_revenue_share ALTER COLUMN id SET DEFAULT nextval('public.cb_manager_revenue_share_id_seq'::regclass);

ALTER TABLE ONLY public.portfolio_rebalance ALTER COLUMN id SET DEFAULT nextval('public.portfolio_rebalance_id_seq'::regclass);

ALTER TABLE ONLY public.portfolio_sleeve ALTER COLUMN id SET DEFAULT nextval('public.portfolio_sleeve_id_seq'::regclass);

ALTER TABLE ONLY public.webhook_event ALTER COLUMN id SET DEFAULT nextval('public.webhook_event_id_seq'::regclass);

ALTER TABLE ONLY public.auth_token
    ADD CONSTRAINT auth_token_hash_key UNIQUE (token_hash);

ALTER TABLE ONLY public.fo_contract_daily
    ADD CONSTRAINT pk_fo_contract_daily PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202201
    ADD CONSTRAINT fo_contract_daily_202201_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202202
    ADD CONSTRAINT fo_contract_daily_202202_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202203
    ADD CONSTRAINT fo_contract_daily_202203_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202204
    ADD CONSTRAINT fo_contract_daily_202204_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202205
    ADD CONSTRAINT fo_contract_daily_202205_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202206
    ADD CONSTRAINT fo_contract_daily_202206_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202207
    ADD CONSTRAINT fo_contract_daily_202207_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202208
    ADD CONSTRAINT fo_contract_daily_202208_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202209
    ADD CONSTRAINT fo_contract_daily_202209_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202210
    ADD CONSTRAINT fo_contract_daily_202210_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202211
    ADD CONSTRAINT fo_contract_daily_202211_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202212
    ADD CONSTRAINT fo_contract_daily_202212_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202301
    ADD CONSTRAINT fo_contract_daily_202301_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202302
    ADD CONSTRAINT fo_contract_daily_202302_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202303
    ADD CONSTRAINT fo_contract_daily_202303_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202304
    ADD CONSTRAINT fo_contract_daily_202304_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202305
    ADD CONSTRAINT fo_contract_daily_202305_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202306
    ADD CONSTRAINT fo_contract_daily_202306_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202307
    ADD CONSTRAINT fo_contract_daily_202307_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202308
    ADD CONSTRAINT fo_contract_daily_202308_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202309
    ADD CONSTRAINT fo_contract_daily_202309_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202310
    ADD CONSTRAINT fo_contract_daily_202310_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202311
    ADD CONSTRAINT fo_contract_daily_202311_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202312
    ADD CONSTRAINT fo_contract_daily_202312_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202401
    ADD CONSTRAINT fo_contract_daily_202401_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202402
    ADD CONSTRAINT fo_contract_daily_202402_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202403
    ADD CONSTRAINT fo_contract_daily_202403_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202404
    ADD CONSTRAINT fo_contract_daily_202404_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202405
    ADD CONSTRAINT fo_contract_daily_202405_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202406
    ADD CONSTRAINT fo_contract_daily_202406_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202407
    ADD CONSTRAINT fo_contract_daily_202407_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202408
    ADD CONSTRAINT fo_contract_daily_202408_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202409
    ADD CONSTRAINT fo_contract_daily_202409_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202410
    ADD CONSTRAINT fo_contract_daily_202410_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202411
    ADD CONSTRAINT fo_contract_daily_202411_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202412
    ADD CONSTRAINT fo_contract_daily_202412_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202501
    ADD CONSTRAINT fo_contract_daily_202501_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202502
    ADD CONSTRAINT fo_contract_daily_202502_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202503
    ADD CONSTRAINT fo_contract_daily_202503_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202504
    ADD CONSTRAINT fo_contract_daily_202504_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202505
    ADD CONSTRAINT fo_contract_daily_202505_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202506
    ADD CONSTRAINT fo_contract_daily_202506_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202507
    ADD CONSTRAINT fo_contract_daily_202507_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202508
    ADD CONSTRAINT fo_contract_daily_202508_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202509
    ADD CONSTRAINT fo_contract_daily_202509_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202510
    ADD CONSTRAINT fo_contract_daily_202510_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202511
    ADD CONSTRAINT fo_contract_daily_202511_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202512
    ADD CONSTRAINT fo_contract_daily_202512_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202601
    ADD CONSTRAINT fo_contract_daily_202601_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202602
    ADD CONSTRAINT fo_contract_daily_202602_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202603
    ADD CONSTRAINT fo_contract_daily_202603_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202604
    ADD CONSTRAINT fo_contract_daily_202604_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202605
    ADD CONSTRAINT fo_contract_daily_202605_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202606
    ADD CONSTRAINT fo_contract_daily_202606_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202607
    ADD CONSTRAINT fo_contract_daily_202607_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202608
    ADD CONSTRAINT fo_contract_daily_202608_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202609
    ADD CONSTRAINT fo_contract_daily_202609_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202610
    ADD CONSTRAINT fo_contract_daily_202610_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202611
    ADD CONSTRAINT fo_contract_daily_202611_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202612
    ADD CONSTRAINT fo_contract_daily_202612_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202701
    ADD CONSTRAINT fo_contract_daily_202701_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202702
    ADD CONSTRAINT fo_contract_daily_202702_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202703
    ADD CONSTRAINT fo_contract_daily_202703_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202704
    ADD CONSTRAINT fo_contract_daily_202704_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202705
    ADD CONSTRAINT fo_contract_daily_202705_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202706
    ADD CONSTRAINT fo_contract_daily_202706_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202707
    ADD CONSTRAINT fo_contract_daily_202707_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202708
    ADD CONSTRAINT fo_contract_daily_202708_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202709
    ADD CONSTRAINT fo_contract_daily_202709_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202710
    ADD CONSTRAINT fo_contract_daily_202710_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202711
    ADD CONSTRAINT fo_contract_daily_202711_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.fo_contract_daily_202712
    ADD CONSTRAINT fo_contract_daily_202712_pkey PRIMARY KEY (trade_date, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.op_chain_snapshot
    ADD CONSTRAINT pk_op_chain_snapshot PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202609
    ADD CONSTRAINT op_chain_snapshot_202609_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202610
    ADD CONSTRAINT op_chain_snapshot_202610_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202611
    ADD CONSTRAINT op_chain_snapshot_202611_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202612
    ADD CONSTRAINT op_chain_snapshot_202612_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202701
    ADD CONSTRAINT op_chain_snapshot_202701_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202702
    ADD CONSTRAINT op_chain_snapshot_202702_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202703
    ADD CONSTRAINT op_chain_snapshot_202703_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202704
    ADD CONSTRAINT op_chain_snapshot_202704_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202705
    ADD CONSTRAINT op_chain_snapshot_202705_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202706
    ADD CONSTRAINT op_chain_snapshot_202706_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202707
    ADD CONSTRAINT op_chain_snapshot_202707_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202708
    ADD CONSTRAINT op_chain_snapshot_202708_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202709
    ADD CONSTRAINT op_chain_snapshot_202709_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202710
    ADD CONSTRAINT op_chain_snapshot_202710_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202711
    ADD CONSTRAINT op_chain_snapshot_202711_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.op_chain_snapshot_202712
    ADD CONSTRAINT op_chain_snapshot_202712_pkey PRIMARY KEY (ts, instrument_token);

ALTER TABLE ONLY public.account_deletion
    ADD CONSTRAINT pk_account_deletion PRIMARY KEY (user_id);

ALTER TABLE ONLY public.admin_action
    ADD CONSTRAINT pk_admin_action PRIMARY KEY (id);

ALTER TABLE ONLY public.api_key
    ADD CONSTRAINT pk_api_key PRIMARY KEY (id);

ALTER TABLE ONLY public.api_key_usage_daily
    ADD CONSTRAINT pk_api_key_usage_daily PRIMARY KEY (api_key_id, date);

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT pk_app_user PRIMARY KEY (id);

ALTER TABLE ONLY public.auth_identity
    ADD CONSTRAINT pk_auth_identity PRIMARY KEY (id);

ALTER TABLE ONLY public.auth_lockout
    ADD CONSTRAINT pk_auth_lockout PRIMARY KEY (identifier);

ALTER TABLE ONLY public.auth_token
    ADD CONSTRAINT pk_auth_token PRIMARY KEY (id);

ALTER TABLE ONLY public.auth_verification_token
    ADD CONSTRAINT pk_auth_verification_token PRIMARY KEY (identifier, token);

ALTER TABLE ONLY public.backtest
    ADD CONSTRAINT pk_backtest PRIMARY KEY (id);

ALTER TABLE ONLY public.basket_snapshot
    ADD CONSTRAINT pk_basket_snapshot PRIMARY KEY (id);

ALTER TABLE ONLY public.broker_account
    ADD CONSTRAINT pk_broker_account PRIMARY KEY (id);

ALTER TABLE ONLY public.broker_cash
    ADD CONSTRAINT pk_broker_cash PRIMARY KEY (broker_account_id);

ALTER TABLE ONLY public.broker_trade
    ADD CONSTRAINT pk_broker_trade PRIMARY KEY (id);

ALTER TABLE ONLY public.candidate_review_label
    ADD CONSTRAINT pk_candidate_review_label PRIMARY KEY (id);

ALTER TABLE ONLY public.catalyst_tag_correction
    ADD CONSTRAINT pk_catalyst_tag_correction PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_basket
    ADD CONSTRAINT pk_cb_basket PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_basket_version
    ADD CONSTRAINT pk_cb_basket_version PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_collection
    ADD CONSTRAINT pk_cb_collection PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_constituent
    ADD CONSTRAINT pk_cb_constituent PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_dividend
    ADD CONSTRAINT pk_cb_dividend PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_fee_ledger
    ADD CONSTRAINT pk_cb_fee_ledger PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_investment
    ADD CONSTRAINT pk_cb_investment PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_investment_holding
    ADD CONSTRAINT pk_cb_investment_holding PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_manager
    ADD CONSTRAINT pk_cb_manager PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_manager_revenue_share
    ADD CONSTRAINT pk_cb_manager_revenue_share PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_metrics
    ADD CONSTRAINT pk_cb_metrics PRIMARY KEY (basket_id, as_of_date);

ALTER TABLE ONLY public.cb_order_batch
    ADD CONSTRAINT pk_cb_order_batch PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_pending_action
    ADD CONSTRAINT pk_cb_pending_action PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_plan
    ADD CONSTRAINT pk_cb_plan PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_sip_plan
    ADD CONSTRAINT pk_cb_sip_plan PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_subscription
    ADD CONSTRAINT pk_cb_subscription PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_update_post
    ADD CONSTRAINT pk_cb_update_post PRIMARY KEY (id);

ALTER TABLE ONLY public.cb_user_rebalance_state
    ADD CONSTRAINT pk_cb_user_rebalance_state PRIMARY KEY (user_id, version_id);

ALTER TABLE ONLY public.cb_watchlist_item
    ADD CONSTRAINT pk_cb_watchlist_item PRIMARY KEY (id);

ALTER TABLE ONLY public.consent_record
    ADD CONSTRAINT pk_consent_record PRIMARY KEY (id);

ALTER TABLE ONLY public.corporate_action
    ADD CONSTRAINT pk_corporate_action PRIMARY KEY (id);

ALTER TABLE ONLY public.desk_score_daily
    ADD CONSTRAINT pk_desk_score_daily PRIMARY KEY (instrument_id, date);

ALTER TABLE ONLY public.entitlement_override
    ADD CONSTRAINT pk_entitlement_override PRIMARY KEY (id);

ALTER TABLE ONLY public.eq_minute_bar
    ADD CONSTRAINT pk_eq_minute_bar PRIMARY KEY (instrument_id, ts);

ALTER TABLE ONLY public.exchange
    ADD CONSTRAINT pk_exchange PRIMARY KEY (id);

ALTER TABLE ONLY public.factor_daily
    ADD CONSTRAINT pk_factor_daily PRIMARY KEY (instrument_id, date);

ALTER TABLE ONLY public.fo_backtest_run
    ADD CONSTRAINT pk_fo_backtest_run PRIMARY KEY (id);

ALTER TABLE ONLY public.fo_book_config
    ADD CONSTRAINT pk_fo_book_config PRIMARY KEY (user_id);

ALTER TABLE ONLY public.fo_config_audit
    ADD CONSTRAINT pk_fo_config_audit PRIMARY KEY (id);

ALTER TABLE ONLY public.fo_fill
    ADD CONSTRAINT pk_fo_fill PRIMARY KEY (id);

ALTER TABLE ONLY public.fo_index_daily
    ADD CONSTRAINT pk_fo_index_daily PRIMARY KEY (underlying, trade_date);

ALTER TABLE ONLY public.fo_ingest_day
    ADD CONSTRAINT pk_fo_ingest_day PRIMARY KEY (trade_date);

ALTER TABLE ONLY public.fo_journal
    ADD CONSTRAINT pk_fo_journal PRIMARY KEY (position_id);

ALTER TABLE ONLY public.fo_leg
    ADD CONSTRAINT pk_fo_leg PRIMARY KEY (id);

ALTER TABLE ONLY public.fo_mark
    ADD CONSTRAINT pk_fo_mark PRIMARY KEY (position_id, trade_date);

ALTER TABLE ONLY public.fo_plan
    ADD CONSTRAINT pk_fo_plan PRIMARY KEY (id);

ALTER TABLE ONLY public.fo_position
    ADD CONSTRAINT pk_fo_position PRIMARY KEY (id);

ALTER TABLE ONLY public.fo_scan
    ADD CONSTRAINT pk_fo_scan PRIMARY KEY (id);

ALTER TABLE ONLY public.fo_sleeve_config
    ADD CONSTRAINT pk_fo_sleeve_config PRIMARY KEY (user_id, sleeve);

ALTER TABLE ONLY public.fo_spread_sample
    ADD CONSTRAINT pk_fo_spread_sample PRIMARY KEY (id);

ALTER TABLE ONLY public.fo_underlying_daily
    ADD CONSTRAINT pk_fo_underlying_daily PRIMARY KEY (trade_date, symbol);

ALTER TABLE ONLY public.fundamental_daily
    ADD CONSTRAINT pk_fundamental_daily PRIMARY KEY (instrument_id, date);

ALTER TABLE ONLY public.index_bar_daily
    ADD CONSTRAINT pk_index_bar_daily PRIMARY KEY (index_id, date);

ALTER TABLE ONLY public.index_constituents
    ADD CONSTRAINT pk_index_constituents PRIMARY KEY (index_id, symbol);

ALTER TABLE ONLY public.index_def
    ADD CONSTRAINT pk_index_def PRIMARY KEY (id);

ALTER TABLE ONLY public.index_master
    ADD CONSTRAINT pk_index_master PRIMARY KEY (id);

ALTER TABLE ONLY public.index_member_daily
    ADD CONSTRAINT pk_index_member_daily PRIMARY KEY (index_id, date, instrument_id);

ALTER TABLE ONLY public.index_snapshot_daily
    ADD CONSTRAINT pk_index_snapshot_daily PRIMARY KEY (index_id, date);

ALTER TABLE ONLY public.index_stats_daily
    ADD CONSTRAINT pk_index_stats_daily PRIMARY KEY (index_id, date);

ALTER TABLE ONLY public.ingest_cursor
    ADD CONSTRAINT pk_ingest_cursor PRIMARY KEY (kind, instrument_id, window_start);

ALTER TABLE ONLY public.instrument
    ADD CONSTRAINT pk_instrument PRIMARY KEY (id);

ALTER TABLE ONLY public.instrument_watch_item
    ADD CONSTRAINT pk_instrument_watch_item PRIMARY KEY (id);

ALTER TABLE ONLY public.invoice_counter
    ADD CONSTRAINT pk_invoice_counter PRIMARY KEY (series);

ALTER TABLE ONLY public.lv_adoption
    ADD CONSTRAINT pk_lv_adoption PRIMARY KEY (id);

ALTER TABLE ONLY public.lv_exit_order
    ADD CONSTRAINT pk_lv_exit_order PRIMARY KEY (id);

ALTER TABLE ONLY public.lv_heartbeat
    ADD CONSTRAINT pk_lv_heartbeat PRIMARY KEY (user_id, process);

ALTER TABLE ONLY public.lv_protection_issue
    ADD CONSTRAINT pk_lv_protection_issue PRIMARY KEY (id);

ALTER TABLE ONLY public.market_health_daily
    ADD CONSTRAINT pk_market_health_daily PRIMARY KEY (index_id, date);

ALTER TABLE ONLY public.mtf_leverage
    ADD CONSTRAINT pk_mtf_leverage PRIMARY KEY (as_of, symbol);

ALTER TABLE ONLY public.ohlcv_daily
    ADD CONSTRAINT pk_ohlcv_daily PRIMARY KEY (instrument_id, date);

ALTER TABLE ONLY public.op_backtest_run
    ADD CONSTRAINT pk_op_backtest_run PRIMARY KEY (id);

ALTER TABLE ONLY public.op_book_config
    ADD CONSTRAINT pk_op_book_config PRIMARY KEY (user_id);

ALTER TABLE ONLY public.op_config_audit
    ADD CONSTRAINT pk_op_config_audit PRIMARY KEY (id);

ALTER TABLE ONLY public.op_contract
    ADD CONSTRAINT pk_op_contract PRIMARY KEY (instrument_token);

ALTER TABLE ONLY public.op_event_day
    ADD CONSTRAINT pk_op_event_day PRIMARY KEY (user_id, date);

ALTER TABLE ONLY public.op_expiry
    ADD CONSTRAINT pk_op_expiry PRIMARY KEY (underlying, expiry_date);

ALTER TABLE ONLY public.op_fill
    ADD CONSTRAINT pk_op_fill PRIMARY KEY (id);

ALTER TABLE ONLY public.op_index_minute
    ADD CONSTRAINT pk_op_index_minute PRIMARY KEY (instrument_id, ts);

ALTER TABLE ONLY public.op_journal
    ADD CONSTRAINT pk_op_journal PRIMARY KEY (session_id);

ALTER TABLE ONLY public.op_leg
    ADD CONSTRAINT pk_op_leg PRIMARY KEY (id);

ALTER TABLE ONLY public.op_order
    ADD CONSTRAINT pk_op_order PRIMARY KEY (id);

ALTER TABLE ONLY public.op_plan
    ADD CONSTRAINT pk_op_plan PRIMARY KEY (id);

ALTER TABLE ONLY public.op_position
    ADD CONSTRAINT pk_op_position PRIMARY KEY (session_id);

ALTER TABLE ONLY public.op_scan
    ADD CONSTRAINT pk_op_scan PRIMARY KEY (id);

ALTER TABLE ONLY public.op_session
    ADD CONSTRAINT pk_op_session PRIMARY KEY (id);

ALTER TABLE ONLY public.op_sleeve_config
    ADD CONSTRAINT pk_op_sleeve_config PRIMARY KEY (user_id, sleeve);

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT pk_payment PRIMARY KEY (id);

ALTER TABLE ONLY public.pipeline_run
    ADD CONSTRAINT pk_pipeline_run PRIMARY KEY (id);

ALTER TABLE ONLY public.pipeline_run_step
    ADD CONSTRAINT pk_pipeline_run_step PRIMARY KEY (id);

ALTER TABLE ONLY public.plan
    ADD CONSTRAINT pk_plan PRIMARY KEY (id);

ALTER TABLE ONLY public.portfolio
    ADD CONSTRAINT pk_portfolio PRIMARY KEY (id);

ALTER TABLE ONLY public.portfolio_cash_flow
    ADD CONSTRAINT pk_portfolio_cash_flow PRIMARY KEY (id);

ALTER TABLE ONLY public.portfolio_holding
    ADD CONSTRAINT pk_portfolio_holding PRIMARY KEY (portfolio_id, instrument_id, broker_account_id);

ALTER TABLE ONLY public.portfolio_rebalance
    ADD CONSTRAINT pk_portfolio_rebalance PRIMARY KEY (id);

ALTER TABLE ONLY public.portfolio_sleeve
    ADD CONSTRAINT pk_portfolio_sleeve PRIMARY KEY (id);

ALTER TABLE ONLY public.reconciliation_item
    ADD CONSTRAINT pk_reconciliation_item PRIMARY KEY (id);

ALTER TABLE ONLY public.refresh_token
    ADD CONSTRAINT pk_refresh_token PRIMARY KEY (id);

ALTER TABLE ONLY public.risk_ledger
    ADD CONSTRAINT pk_risk_ledger PRIMARY KEY (user_id, day);

ALTER TABLE ONLY public.screen
    ADD CONSTRAINT pk_screen PRIMARY KEY (id);

ALTER TABLE ONLY public.screen_alert
    ADD CONSTRAINT pk_screen_alert PRIMARY KEY (id);

ALTER TABLE ONLY public.screen_alert_delivery
    ADD CONSTRAINT pk_screen_alert_delivery PRIMARY KEY (id);

ALTER TABLE ONLY public.screen_run
    ADD CONSTRAINT pk_screen_run PRIMARY KEY (id);

ALTER TABLE ONLY public.sector_index_map
    ADD CONSTRAINT pk_sector_index_map PRIMARY KEY (industry, index_name);

ALTER TABLE ONLY public.stock_regime_signal
    ADD CONSTRAINT pk_stock_regime_signal PRIMARY KEY (as_of, symbol);

ALTER TABLE ONLY public.subscription
    ADD CONSTRAINT pk_subscription PRIMARY KEY (id);

ALTER TABLE ONLY public.sw_backtest_run
    ADD CONSTRAINT pk_sw_backtest_run PRIMARY KEY (id);

ALTER TABLE ONLY public.sw_catalyst
    ADD CONSTRAINT pk_sw_catalyst PRIMARY KEY (id);

ALTER TABLE ONLY public.sw_config
    ADD CONSTRAINT pk_sw_config PRIMARY KEY (user_id);

ALTER TABLE ONLY public.sw_config_audit
    ADD CONSTRAINT pk_sw_config_audit PRIMARY KEY (id);

ALTER TABLE ONLY public.sw_fill
    ADD CONSTRAINT pk_sw_fill PRIMARY KEY (id);

ALTER TABLE ONLY public.sw_market_daily
    ADD CONSTRAINT pk_sw_market_daily PRIMARY KEY (user_id, date);

ALTER TABLE ONLY public.sw_plan
    ADD CONSTRAINT pk_sw_plan PRIMARY KEY (id);

ALTER TABLE ONLY public.sw_plan_line
    ADD CONSTRAINT pk_sw_plan_line PRIMARY KEY (id);

ALTER TABLE ONLY public.sw_plan_skip
    ADD CONSTRAINT pk_sw_plan_skip PRIMARY KEY (id);

ALTER TABLE ONLY public.sw_position
    ADD CONSTRAINT pk_sw_position PRIMARY KEY (id);

ALTER TABLE ONLY public.sw_scan_run
    ADD CONSTRAINT pk_sw_scan_run PRIMARY KEY (id);

ALTER TABLE ONLY public.sw_session
    ADD CONSTRAINT pk_sw_session PRIMARY KEY (user_id, session_date);

ALTER TABLE ONLY public.sw_setup_daily
    ADD CONSTRAINT pk_sw_setup_daily PRIMARY KEY (user_id, date, instrument_id, setup);

ALTER TABLE ONLY public.sw_signal
    ADD CONSTRAINT pk_sw_signal PRIMARY KEY (id);

ALTER TABLE ONLY public.sw_watch
    ADD CONSTRAINT pk_sw_watch PRIMARY KEY (id);

ALTER TABLE ONLY public.swing_candidates
    ADD CONSTRAINT pk_swing_candidates PRIMARY KEY (id);

ALTER TABLE ONLY public.swing_regime_run
    ADD CONSTRAINT pk_swing_regime_run PRIMARY KEY (id);

ALTER TABLE ONLY public.symbol_alias
    ADD CONSTRAINT pk_symbol_alias PRIMARY KEY (id);

ALTER TABLE ONLY public.trading_day
    ADD CONSTRAINT pk_trading_day PRIMARY KEY (exchange_id, date);

ALTER TABLE ONLY public.tw_backtest_run
    ADD CONSTRAINT pk_tw_backtest_run PRIMARY KEY (id);

ALTER TABLE ONLY public.tw_breadth_daily
    ADD CONSTRAINT pk_tw_breadth_daily PRIMARY KEY (user_id, date);

ALTER TABLE ONLY public.tw_config
    ADD CONSTRAINT pk_tw_config PRIMARY KEY (user_id);

ALTER TABLE ONLY public.tw_config_audit
    ADD CONSTRAINT pk_tw_config_audit PRIMARY KEY (id);

ALTER TABLE ONLY public.tw_fill
    ADD CONSTRAINT pk_tw_fill PRIMARY KEY (id);

ALTER TABLE ONLY public.tw_order
    ADD CONSTRAINT pk_tw_order PRIMARY KEY (id);

ALTER TABLE ONLY public.tw_plan
    ADD CONSTRAINT pk_tw_plan PRIMARY KEY (id);

ALTER TABLE ONLY public.tw_plan_line
    ADD CONSTRAINT pk_tw_plan_line PRIMARY KEY (id);

ALTER TABLE ONLY public.tw_plan_skip
    ADD CONSTRAINT pk_tw_plan_skip PRIMARY KEY (id);

ALTER TABLE ONLY public.tw_position
    ADD CONSTRAINT pk_tw_position PRIMARY KEY (id);

ALTER TABLE ONLY public.tw_scan_run
    ADD CONSTRAINT pk_tw_scan_run PRIMARY KEY (id);

ALTER TABLE ONLY public.tw_session
    ADD CONSTRAINT pk_tw_session PRIMARY KEY (user_id, session_date);

ALTER TABLE ONLY public.tw_signal_daily
    ADD CONSTRAINT pk_tw_signal_daily PRIMARY KEY (user_id, date, instrument_id);

ALTER TABLE ONLY public.tw_state_daily
    ADD CONSTRAINT pk_tw_state_daily PRIMARY KEY (user_id, date, instrument_id);

ALTER TABLE ONLY public.user_discover_preferences
    ADD CONSTRAINT pk_user_discover_preferences PRIMARY KEY (user_id);

ALTER TABLE ONLY public.vb_backtest_run
    ADD CONSTRAINT pk_vb_backtest_run PRIMARY KEY (id);

ALTER TABLE ONLY public.vb_breadth_daily
    ADD CONSTRAINT pk_vb_breadth_daily PRIMARY KEY (user_id, date);

ALTER TABLE ONLY public.vb_config
    ADD CONSTRAINT pk_vb_config PRIMARY KEY (user_id);

ALTER TABLE ONLY public.vb_config_audit
    ADD CONSTRAINT pk_vb_config_audit PRIMARY KEY (id);

ALTER TABLE ONLY public.vb_fill
    ADD CONSTRAINT pk_vb_fill PRIMARY KEY (id);

ALTER TABLE ONLY public.vb_order
    ADD CONSTRAINT pk_vb_order PRIMARY KEY (id);

ALTER TABLE ONLY public.vb_plan
    ADD CONSTRAINT pk_vb_plan PRIMARY KEY (id);

ALTER TABLE ONLY public.vb_plan_line
    ADD CONSTRAINT pk_vb_plan_line PRIMARY KEY (id);

ALTER TABLE ONLY public.vb_plan_skip
    ADD CONSTRAINT pk_vb_plan_skip PRIMARY KEY (id);

ALTER TABLE ONLY public.vb_position
    ADD CONSTRAINT pk_vb_position PRIMARY KEY (id);

ALTER TABLE ONLY public.vb_scan_run
    ADD CONSTRAINT pk_vb_scan_run PRIMARY KEY (id);

ALTER TABLE ONLY public.vb_session
    ADD CONSTRAINT pk_vb_session PRIMARY KEY (user_id, session_date);

ALTER TABLE ONLY public.vb_signal_daily
    ADD CONSTRAINT pk_vb_signal_daily PRIMARY KEY (user_id, date, instrument_id);

ALTER TABLE ONLY public.webhook_delivery
    ADD CONSTRAINT pk_webhook_delivery PRIMARY KEY (id);

ALTER TABLE ONLY public.webhook_endpoint
    ADD CONSTRAINT pk_webhook_endpoint PRIMARY KEY (id);

ALTER TABLE ONLY public.webhook_event
    ADD CONSTRAINT pk_webhook_event PRIMARY KEY (id);

ALTER TABLE ONLY public.refresh_token
    ADD CONSTRAINT refresh_token_hash_key UNIQUE (token_hash);

ALTER TABLE ONLY public.api_key
    ADD CONSTRAINT uq_api_key_public_id UNIQUE (public_id);

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT uq_app_user_email UNIQUE (email);

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT uq_app_user_public_id UNIQUE (public_id);

ALTER TABLE ONLY public.auth_identity
    ADD CONSTRAINT uq_auth_identity_provider_subject UNIQUE (provider, subject);

ALTER TABLE ONLY public.auth_identity
    ADD CONSTRAINT uq_auth_identity_user_provider UNIQUE (user_id, provider);

ALTER TABLE ONLY public.backtest
    ADD CONSTRAINT uq_backtest_public_id UNIQUE (public_id);

ALTER TABLE ONLY public.basket_snapshot
    ADD CONSTRAINT uq_basket_snapshot_as_of_version UNIQUE (as_of, data_version);

ALTER TABLE ONLY public.broker_account
    ADD CONSTRAINT uq_broker_account_user_broker UNIQUE (user_id, broker_id);

ALTER TABLE ONLY public.broker_trade
    ADD CONSTRAINT uq_broker_trade_account_trade UNIQUE (broker_account_id, exchange, trade_id);

ALTER TABLE ONLY public.candidate_review_label
    ADD CONSTRAINT uq_candidate_review_label_user_id_review_key UNIQUE (user_id, review_key);

ALTER TABLE ONLY public.catalyst_tag_correction
    ADD CONSTRAINT uq_catalyst_tag_correction_user_id_headline_key UNIQUE (user_id, headline_key);

ALTER TABLE ONLY public.cb_basket
    ADD CONSTRAINT uq_cb_basket_slug UNIQUE (slug);

ALTER TABLE ONLY public.cb_basket_version
    ADD CONSTRAINT uq_cb_basket_version_basket_version_no UNIQUE (basket_id, version_no);

ALTER TABLE ONLY public.cb_collection
    ADD CONSTRAINT uq_cb_collection_slug UNIQUE (slug);

ALTER TABLE ONLY public.cb_manager
    ADD CONSTRAINT uq_cb_manager_slug UNIQUE (slug);

ALTER TABLE ONLY public.cb_watchlist_item
    ADD CONSTRAINT uq_cb_watchlist_item_user_basket UNIQUE (user_id, basket_id);

ALTER TABLE ONLY public.corporate_action
    ADD CONSTRAINT uq_corporate_action_instrument_id_action_type_ex_date UNIQUE (instrument_id, action_type, ex_date);

ALTER TABLE ONLY public.exchange
    ADD CONSTRAINT uq_exchange_code UNIQUE (code);

ALTER TABLE ONLY public.fo_leg
    ADD CONSTRAINT uq_fo_leg_plan_seq UNIQUE (plan_id, entry_seq);

ALTER TABLE ONLY public.fo_plan
    ADD CONSTRAINT uq_fo_plan_plan_id UNIQUE (plan_id);

ALTER TABLE ONLY public.fo_scan
    ADD CONSTRAINT uq_fo_scan_user_sleeve_date_symbol UNIQUE (user_id, sleeve, trade_date, symbol);

ALTER TABLE ONLY public.fo_spread_sample
    ADD CONSTRAINT uq_fo_spread_sample_contract_moment UNIQUE (taken_at, symbol, expiry, strike, option_type);

ALTER TABLE ONLY public.index_def
    ADD CONSTRAINT uq_index_def_slug UNIQUE (slug);

ALTER TABLE ONLY public.index_master
    ADD CONSTRAINT uq_index_master_name UNIQUE (name);

ALTER TABLE ONLY public.instrument
    ADD CONSTRAINT uq_instrument_exchange_id_symbol UNIQUE (exchange_id, symbol);

ALTER TABLE ONLY public.instrument_watch_item
    ADD CONSTRAINT uq_instrument_watch_item_user_instrument UNIQUE (user_id, instrument_id);

ALTER TABLE ONLY public.op_leg
    ADD CONSTRAINT uq_op_leg_plan_seq UNIQUE (plan_id, seq);

ALTER TABLE ONLY public.op_order
    ADD CONSTRAINT uq_op_order_client_id UNIQUE (client_id);

ALTER TABLE ONLY public.op_plan
    ADD CONSTRAINT uq_op_plan_plan_id UNIQUE (plan_id);

ALTER TABLE ONLY public.op_scan
    ADD CONSTRAINT uq_op_scan_user_sleeve_ts UNIQUE (user_id, sleeve, ts);

ALTER TABLE ONLY public.op_session
    ADD CONSTRAINT uq_op_session_user_sleeve_date UNIQUE (user_id, sleeve, trade_date);

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT uq_payment_invoice_number UNIQUE (invoice_number);

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT uq_payment_razorpay_payment_id UNIQUE (razorpay_payment_id);

ALTER TABLE ONLY public.pipeline_run_step
    ADD CONSTRAINT uq_pipeline_run_step_run_id_step UNIQUE (run_id, step);

ALTER TABLE ONLY public.plan
    ADD CONSTRAINT uq_plan_code UNIQUE (code);

ALTER TABLE ONLY public.portfolio
    ADD CONSTRAINT uq_portfolio_id_kind UNIQUE (id, kind);

ALTER TABLE ONLY public.portfolio_sleeve
    ADD CONSTRAINT uq_portfolio_sleeve_name UNIQUE (portfolio_id, name);

ALTER TABLE ONLY public.screen_alert_delivery
    ADD CONSTRAINT uq_screen_alert_delivery_alert_id_as_of UNIQUE (alert_id, as_of);

ALTER TABLE ONLY public.screen_alert
    ADD CONSTRAINT uq_screen_alert_public_id UNIQUE (public_id);

ALTER TABLE ONLY public.screen_alert
    ADD CONSTRAINT uq_screen_alert_user_id_screen_id UNIQUE (user_id, screen_id);

ALTER TABLE ONLY public.screen
    ADD CONSTRAINT uq_screen_public_id UNIQUE (public_id);

ALTER TABLE ONLY public.screen_run
    ADD CONSTRAINT uq_screen_run_screen_id_as_of_definition_hash UNIQUE (screen_id, as_of, definition_hash);

ALTER TABLE ONLY public.sw_catalyst
    ADD CONSTRAINT uq_sw_catalyst_user_id_instrument_id_url UNIQUE (user_id, instrument_id, url);

ALTER TABLE ONLY public.sw_plan_line
    ADD CONSTRAINT uq_sw_plan_line_client_id UNIQUE (client_id);

ALTER TABLE ONLY public.sw_plan
    ADD CONSTRAINT uq_sw_plan_plan_id UNIQUE (plan_id);

ALTER TABLE ONLY public.sw_watch
    ADD CONSTRAINT uq_sw_watch_user_id_instrument_id_setup_added_on UNIQUE (user_id, instrument_id, setup, added_on);

ALTER TABLE ONLY public.tw_order
    ADD CONSTRAINT uq_tw_order_one_per_signal UNIQUE (user_id, signal_date, instrument_id, side);

ALTER TABLE ONLY public.tw_plan_line
    ADD CONSTRAINT uq_tw_plan_line_once UNIQUE (plan_id, instrument_id, kind);

ALTER TABLE ONLY public.tw_plan
    ADD CONSTRAINT uq_tw_plan_plan_id UNIQUE (plan_id);

ALTER TABLE ONLY public.vb_order
    ADD CONSTRAINT uq_vb_order_one_per_signal UNIQUE (user_id, signal_date, instrument_id);

ALTER TABLE ONLY public.vb_plan_line
    ADD CONSTRAINT uq_vb_plan_line_once UNIQUE (plan_id, instrument_id, kind);

ALTER TABLE ONLY public.vb_plan
    ADD CONSTRAINT uq_vb_plan_plan_id UNIQUE (plan_id);

ALTER TABLE ONLY public.webhook_delivery
    ADD CONSTRAINT uq_webhook_delivery_endpoint_id_key UNIQUE (endpoint_id, idempotency_key);

ALTER TABLE ONLY public.webhook_endpoint
    ADD CONSTRAINT uq_webhook_endpoint_public_id UNIQUE (public_id);

ALTER TABLE ONLY public.webhook_endpoint
    ADD CONSTRAINT uq_webhook_endpoint_target UNIQUE (user_id, screen_id, url);

ALTER TABLE ONLY public.webhook_event
    ADD CONSTRAINT uq_webhook_event_event_id UNIQUE (event_id);

CREATE INDEX eq_minute_bar_ts_idx ON public.eq_minute_bar USING btree (ts DESC);

CREATE INDEX ix_fo_contract_daily_symbol_date ON ONLY public.fo_contract_daily USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202201_symbol_trade_date_idx ON public.fo_contract_daily_202201 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202202_symbol_trade_date_idx ON public.fo_contract_daily_202202 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202203_symbol_trade_date_idx ON public.fo_contract_daily_202203 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202204_symbol_trade_date_idx ON public.fo_contract_daily_202204 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202205_symbol_trade_date_idx ON public.fo_contract_daily_202205 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202206_symbol_trade_date_idx ON public.fo_contract_daily_202206 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202207_symbol_trade_date_idx ON public.fo_contract_daily_202207 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202208_symbol_trade_date_idx ON public.fo_contract_daily_202208 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202209_symbol_trade_date_idx ON public.fo_contract_daily_202209 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202210_symbol_trade_date_idx ON public.fo_contract_daily_202210 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202211_symbol_trade_date_idx ON public.fo_contract_daily_202211 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202212_symbol_trade_date_idx ON public.fo_contract_daily_202212 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202301_symbol_trade_date_idx ON public.fo_contract_daily_202301 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202302_symbol_trade_date_idx ON public.fo_contract_daily_202302 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202303_symbol_trade_date_idx ON public.fo_contract_daily_202303 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202304_symbol_trade_date_idx ON public.fo_contract_daily_202304 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202305_symbol_trade_date_idx ON public.fo_contract_daily_202305 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202306_symbol_trade_date_idx ON public.fo_contract_daily_202306 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202307_symbol_trade_date_idx ON public.fo_contract_daily_202307 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202308_symbol_trade_date_idx ON public.fo_contract_daily_202308 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202309_symbol_trade_date_idx ON public.fo_contract_daily_202309 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202310_symbol_trade_date_idx ON public.fo_contract_daily_202310 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202311_symbol_trade_date_idx ON public.fo_contract_daily_202311 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202312_symbol_trade_date_idx ON public.fo_contract_daily_202312 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202401_symbol_trade_date_idx ON public.fo_contract_daily_202401 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202402_symbol_trade_date_idx ON public.fo_contract_daily_202402 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202403_symbol_trade_date_idx ON public.fo_contract_daily_202403 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202404_symbol_trade_date_idx ON public.fo_contract_daily_202404 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202405_symbol_trade_date_idx ON public.fo_contract_daily_202405 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202406_symbol_trade_date_idx ON public.fo_contract_daily_202406 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202407_symbol_trade_date_idx ON public.fo_contract_daily_202407 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202408_symbol_trade_date_idx ON public.fo_contract_daily_202408 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202409_symbol_trade_date_idx ON public.fo_contract_daily_202409 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202410_symbol_trade_date_idx ON public.fo_contract_daily_202410 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202411_symbol_trade_date_idx ON public.fo_contract_daily_202411 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202412_symbol_trade_date_idx ON public.fo_contract_daily_202412 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202501_symbol_trade_date_idx ON public.fo_contract_daily_202501 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202502_symbol_trade_date_idx ON public.fo_contract_daily_202502 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202503_symbol_trade_date_idx ON public.fo_contract_daily_202503 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202504_symbol_trade_date_idx ON public.fo_contract_daily_202504 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202505_symbol_trade_date_idx ON public.fo_contract_daily_202505 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202506_symbol_trade_date_idx ON public.fo_contract_daily_202506 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202507_symbol_trade_date_idx ON public.fo_contract_daily_202507 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202508_symbol_trade_date_idx ON public.fo_contract_daily_202508 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202509_symbol_trade_date_idx ON public.fo_contract_daily_202509 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202510_symbol_trade_date_idx ON public.fo_contract_daily_202510 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202511_symbol_trade_date_idx ON public.fo_contract_daily_202511 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202512_symbol_trade_date_idx ON public.fo_contract_daily_202512 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202601_symbol_trade_date_idx ON public.fo_contract_daily_202601 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202602_symbol_trade_date_idx ON public.fo_contract_daily_202602 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202603_symbol_trade_date_idx ON public.fo_contract_daily_202603 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202604_symbol_trade_date_idx ON public.fo_contract_daily_202604 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202605_symbol_trade_date_idx ON public.fo_contract_daily_202605 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202606_symbol_trade_date_idx ON public.fo_contract_daily_202606 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202607_symbol_trade_date_idx ON public.fo_contract_daily_202607 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202608_symbol_trade_date_idx ON public.fo_contract_daily_202608 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202609_symbol_trade_date_idx ON public.fo_contract_daily_202609 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202610_symbol_trade_date_idx ON public.fo_contract_daily_202610 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202611_symbol_trade_date_idx ON public.fo_contract_daily_202611 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202612_symbol_trade_date_idx ON public.fo_contract_daily_202612 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202701_symbol_trade_date_idx ON public.fo_contract_daily_202701 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202702_symbol_trade_date_idx ON public.fo_contract_daily_202702 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202703_symbol_trade_date_idx ON public.fo_contract_daily_202703 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202704_symbol_trade_date_idx ON public.fo_contract_daily_202704 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202705_symbol_trade_date_idx ON public.fo_contract_daily_202705 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202706_symbol_trade_date_idx ON public.fo_contract_daily_202706 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202707_symbol_trade_date_idx ON public.fo_contract_daily_202707 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202708_symbol_trade_date_idx ON public.fo_contract_daily_202708 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202709_symbol_trade_date_idx ON public.fo_contract_daily_202709 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202710_symbol_trade_date_idx ON public.fo_contract_daily_202710 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202711_symbol_trade_date_idx ON public.fo_contract_daily_202711 USING btree (symbol, trade_date);

CREATE INDEX fo_contract_daily_202712_symbol_trade_date_idx ON public.fo_contract_daily_202712 USING btree (symbol, trade_date);

CREATE INDEX index_snapshot_daily_date_idx ON public.index_snapshot_daily USING btree (date DESC);

CREATE INDEX ix_admin_action_created_at ON public.admin_action USING btree (created_at);

CREATE INDEX ix_api_key_user_id ON public.api_key USING btree (user_id);

CREATE INDEX ix_auth_identity_user_id ON public.auth_identity USING btree (user_id);

CREATE INDEX ix_auth_token_email_purpose ON public.auth_token USING btree (email, purpose);

CREATE INDEX ix_backtest_status_started_at ON public.backtest USING btree (status, started_at);

CREATE INDEX ix_basket_snapshot_as_of ON public.basket_snapshot USING btree (as_of);

CREATE INDEX ix_broker_account_user_id ON public.broker_account USING btree (user_id);

CREATE INDEX ix_broker_trade_user_instrument_date ON public.broker_trade USING btree (user_id, instrument_id, trade_date);

CREATE INDEX ix_cb_basket_manager_id ON public.cb_basket USING btree (manager_id);

CREATE INDEX ix_cb_basket_source_screen_id ON public.cb_basket USING btree (source_screen_id);

CREATE INDEX ix_cb_basket_version_basket_id ON public.cb_basket_version USING btree (basket_id);

CREATE INDEX ix_cb_constituent_instrument_id ON public.cb_constituent USING btree (instrument_id);

CREATE INDEX ix_cb_constituent_version_id ON public.cb_constituent USING btree (version_id);

CREATE INDEX ix_cb_dividend_investment_id ON public.cb_dividend USING btree (investment_id);

CREATE INDEX ix_cb_fee_ledger_user_id ON public.cb_fee_ledger USING btree (user_id);

CREATE INDEX ix_cb_investment_basket_id ON public.cb_investment USING btree (basket_id);

CREATE INDEX ix_cb_investment_broker_account_id ON public.cb_investment USING btree (broker_account_id);

CREATE INDEX ix_cb_investment_holding_instrument_id ON public.cb_investment_holding USING btree (instrument_id);

CREATE INDEX ix_cb_investment_holding_investment_id ON public.cb_investment_holding USING btree (investment_id);

CREATE INDEX ix_cb_investment_portfolio_id ON public.cb_investment USING btree (portfolio_id);

CREATE INDEX ix_cb_investment_user_id ON public.cb_investment USING btree (user_id);

CREATE INDEX ix_cb_manager_revenue_share_manager_id ON public.cb_manager_revenue_share USING btree (manager_id);

CREATE INDEX ix_cb_order_batch_broker_account_id ON public.cb_order_batch USING btree (broker_account_id);

CREATE INDEX ix_cb_order_batch_investment_id ON public.cb_order_batch USING btree (investment_id);

CREATE INDEX ix_cb_order_batch_user_id ON public.cb_order_batch USING btree (user_id);

CREATE INDEX ix_cb_pending_action_user_id ON public.cb_pending_action USING btree (user_id);

CREATE INDEX ix_cb_sip_plan_investment_id ON public.cb_sip_plan USING btree (investment_id);

CREATE INDEX ix_cb_subscription_user_id ON public.cb_subscription USING btree (user_id);

CREATE INDEX ix_cb_watchlist_item_user_id ON public.cb_watchlist_item USING btree (user_id);

CREATE INDEX ix_consent_record_user ON public.consent_record USING btree (user_id);

CREATE INDEX ix_desk_score_daily_date_score_rank ON public.desk_score_daily USING btree (date, score_rank);

CREATE INDEX ix_eq_minute_bar_ts ON public.eq_minute_bar USING btree (ts);

CREATE INDEX ix_factor_daily_date_marketcap_cr ON public.factor_daily USING btree (date, marketcap_cr) INCLUDE (instrument_id);

CREATE INDEX ix_fo_backtest_run_user_family_time ON public.fo_backtest_run USING btree (user_id, family, run_at);

CREATE INDEX ix_fo_config_audit_user_time ON public.fo_config_audit USING btree (user_id, changed_at);

CREATE INDEX ix_fo_plan_user_date_sleeve ON public.fo_plan USING btree (user_id, trade_date, sleeve);

CREATE INDEX ix_fo_position_user_open ON public.fo_position USING btree (user_id, closed_at);

CREATE INDEX ix_fo_spread_sample_symbol_date ON public.fo_spread_sample USING btree (symbol, trade_date);

CREATE INDEX ix_fo_underlying_daily_symbol_date ON public.fo_underlying_daily USING btree (symbol, trade_date);

CREATE INDEX ix_index_constituents_symbol ON public.index_constituents USING btree (symbol);

CREATE INDEX ix_index_member_daily_date_index_id ON public.index_member_daily USING btree (date, index_id);

CREATE INDEX ix_index_member_daily_date_instrument_id ON public.index_member_daily USING btree (date, instrument_id);

CREATE INDEX ix_ingest_cursor_kind_status ON public.ingest_cursor USING btree (kind, status);

CREATE INDEX ix_instrument_kite_token ON public.instrument USING btree (kite_token);

CREATE INDEX ix_instrument_listings_page ON public.instrument USING btree (COALESCE(listed_on, '0001-01-01'::date) DESC, symbol) WHERE (is_active IS TRUE);

CREATE INDEX ix_instrument_symbol ON public.instrument USING btree (symbol);

CREATE INDEX ix_instrument_watch_item_user_id ON public.instrument_watch_item USING btree (user_id);

CREATE INDEX ix_lv_exit_order_broker ON public.lv_exit_order USING btree (user_id, broker_order_id);

CREATE INDEX ix_lv_exit_order_open ON public.lv_exit_order USING btree (user_id, sleeve, state);

CREATE INDEX ix_lv_protection_issue_open_by_sleeve ON public.lv_protection_issue USING btree (user_id, sleeve, resolved_at);

CREATE INDEX ix_op_chain_snapshot_expiry_ts ON ONLY public.op_chain_snapshot USING btree (expiry, ts);

CREATE INDEX ix_op_chain_snapshot_ts_brin ON ONLY public.op_chain_snapshot USING brin (ts);

CREATE INDEX ix_op_config_audit_user_time ON public.op_config_audit USING btree (user_id, changed_at);

CREATE INDEX ix_op_contract_underlying_expiry ON public.op_contract USING btree (underlying, expiry);

CREATE INDEX ix_op_scan_user_date_sleeve ON public.op_scan USING btree (user_id, trade_date, sleeve);

CREATE INDEX ix_payment_user_id_created_at ON public.payment USING btree (user_id, created_at);

CREATE INDEX ix_portfolio_broker_account_id ON public.portfolio USING btree (broker_account_id);

CREATE INDEX ix_portfolio_cash_flow_broker_occurred ON public.portfolio_cash_flow USING btree (broker_account_id, occurred_on);

CREATE INDEX ix_portfolio_cash_flow_portfolio_occurred ON public.portfolio_cash_flow USING btree (portfolio_id, occurred_on);

CREATE INDEX ix_portfolio_holding_broker_account_id ON public.portfolio_holding USING btree (broker_account_id);

CREATE INDEX ix_portfolio_holding_instrument_id ON public.portfolio_holding USING btree (instrument_id);

CREATE INDEX ix_portfolio_nav_daily_portfolio_date ON public.portfolio_nav_daily USING btree (portfolio_id, date);

CREATE INDEX ix_portfolio_parent_id ON public.portfolio USING btree (parent_id);

CREATE INDEX ix_portfolio_rebalance_portfolio_id ON public.portfolio_rebalance USING btree (portfolio_id);

CREATE INDEX ix_portfolio_sleeve_basket_id ON public.portfolio_sleeve USING btree (basket_id);

CREATE INDEX ix_portfolio_sleeve_portfolio ON public.portfolio_sleeve USING btree (portfolio_id);

CREATE INDEX ix_reconciliation_item_user_state ON public.reconciliation_item USING btree (user_id, state);

CREATE INDEX ix_refresh_token_family ON public.refresh_token USING btree (family_id);

CREATE INDEX ix_refresh_token_user ON public.refresh_token USING btree (user_id);

CREATE INDEX ix_screen_user_id ON public.screen USING btree (user_id);

CREATE INDEX ix_stock_regime_signal_symbol ON public.stock_regime_signal USING btree (symbol);

CREATE INDEX ix_sw_backtest_run_user_id_finished_at ON public.sw_backtest_run USING btree (user_id, finished_at);

CREATE INDEX ix_sw_catalyst_user_id_instrument_id_published_at ON public.sw_catalyst USING btree (user_id, instrument_id, published_at);

CREATE INDEX ix_sw_config_audit_user_id_changed_at ON public.sw_config_audit USING btree (user_id, changed_at);

CREATE INDEX ix_sw_fill_position_id ON public.sw_fill USING btree (position_id);

CREATE INDEX ix_sw_plan_line_plan_id ON public.sw_plan_line USING btree (plan_id);

CREATE INDEX ix_sw_plan_skip_plan_id ON public.sw_plan_skip USING btree (plan_id);

CREATE INDEX ix_sw_plan_user_id_as_of ON public.sw_plan USING btree (user_id, as_of);

CREATE INDEX ix_sw_position_instrument_id ON public.sw_position USING btree (instrument_id);

CREATE INDEX ix_sw_position_user_id_state ON public.sw_position USING btree (user_id, state);

CREATE INDEX ix_sw_scan_run_user_id_requested_at ON public.sw_scan_run USING btree (user_id, requested_at);

CREATE INDEX ix_sw_setup_daily_date_setup_score ON public.sw_setup_daily USING btree (date, setup, score);

CREATE INDEX ix_sw_setup_daily_instrument_id_date ON public.sw_setup_daily USING btree (instrument_id, date);

CREATE INDEX ix_sw_signal_instrument_id_session_date ON public.sw_signal USING btree (instrument_id, session_date);

CREATE INDEX ix_sw_signal_user_id_session_date ON public.sw_signal USING btree (user_id, session_date);

CREATE INDEX ix_sw_watch_user_id_state ON public.sw_watch USING btree (user_id, state);

CREATE INDEX ix_swing_candidates_run_at ON public.swing_candidates USING btree (run_at DESC);

CREATE INDEX ix_swing_candidates_symbol ON public.swing_candidates USING btree (symbol);

CREATE INDEX ix_swing_regime_run_mode_run_at ON public.swing_regime_run USING btree (mode, run_at DESC);

CREATE INDEX ix_trading_day_is_trading_day_date ON public.trading_day USING btree (is_trading_day, date);

CREATE INDEX ix_tw_backtest_run_latest ON public.tw_backtest_run USING btree (user_id, source, finished_at);

CREATE INDEX ix_tw_config_audit_user_time ON public.tw_config_audit USING btree (user_id, changed_at);

CREATE INDEX ix_tw_fill_position ON public.tw_fill USING btree (position_id, filled_at);

CREATE INDEX ix_tw_order_state ON public.tw_order USING btree (user_id, state);

CREATE INDEX ix_tw_plan_line_plan ON public.tw_plan_line USING btree (plan_id, kind);

CREATE INDEX ix_tw_plan_session ON public.tw_plan USING btree (user_id, session_date, built_at);

CREATE INDEX ix_tw_plan_skip_plan ON public.tw_plan_skip USING btree (plan_id);

CREATE INDEX ix_tw_position_book ON public.tw_position USING btree (user_id, state, instrument_id);

CREATE INDEX ix_tw_scan_run_user_id_requested_at ON public.tw_scan_run USING btree (user_id, requested_at);

CREATE INDEX ix_tw_signal_daily_instrument ON public.tw_signal_daily USING btree (instrument_id, date);

CREATE INDEX ix_tw_signal_daily_rank ON public.tw_signal_daily USING btree (date, state, rank_key DESC);

CREATE INDEX ix_tw_state_daily_instrument ON public.tw_state_daily USING btree (instrument_id, date);

CREATE INDEX ix_tw_state_daily_session ON public.tw_state_daily USING btree (date, instrument_id);

CREATE INDEX ix_vb_backtest_run_latest ON public.vb_backtest_run USING btree (user_id, source, finished_at);

CREATE INDEX ix_vb_config_audit_user_time ON public.vb_config_audit USING btree (user_id, changed_at);

CREATE INDEX ix_vb_fill_position ON public.vb_fill USING btree (position_id, filled_at);

CREATE INDEX ix_vb_order_sweep ON public.vb_order USING btree (user_id, state, expires_after_session);

CREATE INDEX ix_vb_plan_line_plan ON public.vb_plan_line USING btree (plan_id, kind);

CREATE INDEX ix_vb_plan_session ON public.vb_plan USING btree (user_id, session_date, built_at);

CREATE INDEX ix_vb_plan_skip_plan ON public.vb_plan_skip USING btree (plan_id);

CREATE INDEX ix_vb_position_book ON public.vb_position USING btree (user_id, state, instrument_id);

CREATE INDEX ix_vb_scan_run_user_id_requested_at ON public.vb_scan_run USING btree (user_id, requested_at);

CREATE INDEX ix_vb_signal_daily_instrument ON public.vb_signal_daily USING btree (instrument_id, date);

CREATE INDEX ix_vb_signal_daily_rank ON public.vb_signal_daily USING btree (date, state, rank_key);

CREATE INDEX ix_webhook_delivery_next_attempt_at ON public.webhook_delivery USING btree (status, next_attempt_at);

CREATE INDEX ix_webhook_endpoint_user_id ON public.webhook_endpoint USING btree (user_id);

CREATE INDEX market_health_daily_date_idx ON public.market_health_daily USING btree (date DESC);

CREATE INDEX op_chain_snapshot_202609_expiry_ts_idx ON public.op_chain_snapshot_202609 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202609_ts_idx ON public.op_chain_snapshot_202609 USING brin (ts);

CREATE INDEX op_chain_snapshot_202610_expiry_ts_idx ON public.op_chain_snapshot_202610 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202610_ts_idx ON public.op_chain_snapshot_202610 USING brin (ts);

CREATE INDEX op_chain_snapshot_202611_expiry_ts_idx ON public.op_chain_snapshot_202611 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202611_ts_idx ON public.op_chain_snapshot_202611 USING brin (ts);

CREATE INDEX op_chain_snapshot_202612_expiry_ts_idx ON public.op_chain_snapshot_202612 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202612_ts_idx ON public.op_chain_snapshot_202612 USING brin (ts);

CREATE INDEX op_chain_snapshot_202701_expiry_ts_idx ON public.op_chain_snapshot_202701 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202701_ts_idx ON public.op_chain_snapshot_202701 USING brin (ts);

CREATE INDEX op_chain_snapshot_202702_expiry_ts_idx ON public.op_chain_snapshot_202702 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202702_ts_idx ON public.op_chain_snapshot_202702 USING brin (ts);

CREATE INDEX op_chain_snapshot_202703_expiry_ts_idx ON public.op_chain_snapshot_202703 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202703_ts_idx ON public.op_chain_snapshot_202703 USING brin (ts);

CREATE INDEX op_chain_snapshot_202704_expiry_ts_idx ON public.op_chain_snapshot_202704 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202704_ts_idx ON public.op_chain_snapshot_202704 USING brin (ts);

CREATE INDEX op_chain_snapshot_202705_expiry_ts_idx ON public.op_chain_snapshot_202705 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202705_ts_idx ON public.op_chain_snapshot_202705 USING brin (ts);

CREATE INDEX op_chain_snapshot_202706_expiry_ts_idx ON public.op_chain_snapshot_202706 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202706_ts_idx ON public.op_chain_snapshot_202706 USING brin (ts);

CREATE INDEX op_chain_snapshot_202707_expiry_ts_idx ON public.op_chain_snapshot_202707 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202707_ts_idx ON public.op_chain_snapshot_202707 USING brin (ts);

CREATE INDEX op_chain_snapshot_202708_expiry_ts_idx ON public.op_chain_snapshot_202708 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202708_ts_idx ON public.op_chain_snapshot_202708 USING brin (ts);

CREATE INDEX op_chain_snapshot_202709_expiry_ts_idx ON public.op_chain_snapshot_202709 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202709_ts_idx ON public.op_chain_snapshot_202709 USING brin (ts);

CREATE INDEX op_chain_snapshot_202710_expiry_ts_idx ON public.op_chain_snapshot_202710 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202710_ts_idx ON public.op_chain_snapshot_202710 USING brin (ts);

CREATE INDEX op_chain_snapshot_202711_expiry_ts_idx ON public.op_chain_snapshot_202711 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202711_ts_idx ON public.op_chain_snapshot_202711 USING brin (ts);

CREATE INDEX op_chain_snapshot_202712_expiry_ts_idx ON public.op_chain_snapshot_202712 USING btree (expiry, ts);

CREATE INDEX op_chain_snapshot_202712_ts_idx ON public.op_chain_snapshot_202712 USING brin (ts);

CREATE UNIQUE INDEX uq_api_key_prefix ON public.api_key USING btree (prefix);

CREATE UNIQUE INDEX uq_cb_manager_user_id ON public.cb_manager USING btree (user_id) WHERE (user_id IS NOT NULL);

CREATE UNIQUE INDEX uq_entitlement_override_user_feature ON public.entitlement_override USING btree (user_id, feature);

CREATE UNIQUE INDEX uq_lv_protection_issue_open ON public.lv_protection_issue USING btree (user_id, sleeve, position_id, kind) WHERE (resolved_at IS NULL);

CREATE UNIQUE INDEX uq_portfolio_nav_daily_user_id_date_portfolio_id ON public.portfolio_nav_daily USING btree (user_id, date, portfolio_id) NULLS NOT DISTINCT;

CREATE UNIQUE INDEX uq_portfolio_one_pile_per_broker_account ON public.portfolio USING btree (user_id, broker_account_id) WHERE is_broker_pile;

CREATE UNIQUE INDEX uq_screen_alert_unsubscribe_token_hash ON public.screen_alert USING btree (unsubscribe_token_hash);

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202201_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202201_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202202_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202202_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202203_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202203_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202204_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202204_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202205_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202205_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202206_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202206_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202207_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202207_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202208_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202208_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202209_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202209_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202210_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202210_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202211_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202211_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202212_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202212_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202301_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202301_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202302_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202302_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202303_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202303_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202304_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202304_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202305_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202305_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202306_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202306_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202307_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202307_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202308_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202308_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202309_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202309_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202310_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202310_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202311_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202311_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202312_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202312_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202401_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202401_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202402_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202402_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202403_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202403_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202404_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202404_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202405_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202405_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202406_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202406_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202407_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202407_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202408_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202408_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202409_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202409_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202410_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202410_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202411_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202411_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202412_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202412_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202501_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202501_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202502_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202502_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202503_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202503_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202504_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202504_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202505_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202505_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202506_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202506_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202507_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202507_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202508_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202508_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202509_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202509_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202510_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202510_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202511_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202511_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202512_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202512_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202601_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202601_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202602_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202602_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202603_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202603_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202604_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202604_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202605_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202605_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202606_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202606_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202607_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202607_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202608_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202608_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202609_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202609_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202610_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202610_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202611_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202611_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202612_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202612_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202701_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202701_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202702_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202702_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202703_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202703_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202704_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202704_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202705_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202705_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202706_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202706_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202707_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202707_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202708_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202708_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202709_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202709_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202710_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202710_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202711_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202711_symbol_trade_date_idx;

ALTER INDEX public.pk_fo_contract_daily ATTACH PARTITION public.fo_contract_daily_202712_pkey;

ALTER INDEX public.ix_fo_contract_daily_symbol_date ATTACH PARTITION public.fo_contract_daily_202712_symbol_trade_date_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202609_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202609_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202609_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202610_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202610_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202610_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202611_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202611_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202611_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202612_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202612_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202612_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202701_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202701_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202701_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202702_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202702_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202702_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202703_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202703_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202703_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202704_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202704_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202704_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202705_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202705_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202705_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202706_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202706_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202706_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202707_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202707_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202707_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202708_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202708_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202708_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202709_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202709_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202709_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202710_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202710_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202710_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202711_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202711_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202711_ts_idx;

ALTER INDEX public.ix_op_chain_snapshot_expiry_ts ATTACH PARTITION public.op_chain_snapshot_202712_expiry_ts_idx;

ALTER INDEX public.pk_op_chain_snapshot ATTACH PARTITION public.op_chain_snapshot_202712_pkey;

ALTER INDEX public.ix_op_chain_snapshot_ts_brin ATTACH PARTITION public.op_chain_snapshot_202712_ts_idx;

CREATE TRIGGER portfolio_holding_attribute_broker_account BEFORE INSERT ON public.portfolio_holding FOR EACH ROW EXECUTE FUNCTION public.portfolio_holding_attribute_broker_account();

CREATE TRIGGER portfolio_holding_attribute_kind BEFORE INSERT ON public.portfolio_holding FOR EACH ROW EXECUTE FUNCTION public.portfolio_holding_attribute_kind();

ALTER TABLE ONLY public.account_deletion
    ADD CONSTRAINT fk_account_deletion_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.admin_action
    ADD CONSTRAINT fk_admin_action_actor_user_id_app_user FOREIGN KEY (actor_user_id) REFERENCES public.app_user(id);

ALTER TABLE ONLY public.api_key
    ADD CONSTRAINT fk_api_key_rotated_from_id_api_key FOREIGN KEY (rotated_from_id) REFERENCES public.api_key(id);

ALTER TABLE ONLY public.api_key_usage_daily
    ADD CONSTRAINT fk_api_key_usage_daily_api_key_id_api_key FOREIGN KEY (api_key_id) REFERENCES public.api_key(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.api_key
    ADD CONSTRAINT fk_api_key_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.auth_identity
    ADD CONSTRAINT fk_auth_identity_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id);

ALTER TABLE ONLY public.auth_token
    ADD CONSTRAINT fk_auth_token_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.backtest
    ADD CONSTRAINT fk_backtest_screen_id_screen FOREIGN KEY (screen_id) REFERENCES public.screen(id);

ALTER TABLE ONLY public.backtest
    ADD CONSTRAINT fk_backtest_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id);

ALTER TABLE ONLY public.broker_account
    ADD CONSTRAINT fk_broker_account_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id);

ALTER TABLE ONLY public.broker_cash
    ADD CONSTRAINT fk_broker_cash_broker_account_id_broker_account FOREIGN KEY (broker_account_id) REFERENCES public.broker_account(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.broker_trade
    ADD CONSTRAINT fk_broker_trade_broker_account_id_broker_account FOREIGN KEY (broker_account_id) REFERENCES public.broker_account(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.broker_trade
    ADD CONSTRAINT fk_broker_trade_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.broker_trade
    ADD CONSTRAINT fk_broker_trade_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id);

ALTER TABLE ONLY public.candidate_review_label
    ADD CONSTRAINT fk_candidate_review_label_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.candidate_review_label
    ADD CONSTRAINT fk_candidate_review_label_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.catalyst_tag_correction
    ADD CONSTRAINT fk_catalyst_tag_correction_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_basket
    ADD CONSTRAINT fk_cb_basket_benchmark_instrument_id_instrument FOREIGN KEY (benchmark_instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.cb_basket
    ADD CONSTRAINT fk_cb_basket_manager_id_cb_manager FOREIGN KEY (manager_id) REFERENCES public.cb_manager(id);

ALTER TABLE ONLY public.cb_basket
    ADD CONSTRAINT fk_cb_basket_source_screen_id FOREIGN KEY (source_screen_id) REFERENCES public.screen(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.cb_basket_version
    ADD CONSTRAINT fk_cb_basket_version_basket_id_cb_basket FOREIGN KEY (basket_id) REFERENCES public.cb_basket(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_constituent
    ADD CONSTRAINT fk_cb_constituent_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.cb_constituent
    ADD CONSTRAINT fk_cb_constituent_version_id_cb_basket_version FOREIGN KEY (version_id) REFERENCES public.cb_basket_version(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_dividend
    ADD CONSTRAINT fk_cb_dividend_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.cb_dividend
    ADD CONSTRAINT fk_cb_dividend_investment_id_cb_investment FOREIGN KEY (investment_id) REFERENCES public.cb_investment(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_fee_ledger
    ADD CONSTRAINT fk_cb_fee_ledger_batch_id_cb_order_batch FOREIGN KEY (batch_id) REFERENCES public.cb_order_batch(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_fee_ledger
    ADD CONSTRAINT fk_cb_fee_ledger_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_investment
    ADD CONSTRAINT fk_cb_investment_basket_id_cb_basket FOREIGN KEY (basket_id) REFERENCES public.cb_basket(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_investment
    ADD CONSTRAINT fk_cb_investment_broker_account_id_broker_account FOREIGN KEY (broker_account_id) REFERENCES public.broker_account(id);

ALTER TABLE ONLY public.cb_investment_holding
    ADD CONSTRAINT fk_cb_investment_holding_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.cb_investment_holding
    ADD CONSTRAINT fk_cb_investment_holding_investment_id_cb_investment FOREIGN KEY (investment_id) REFERENCES public.cb_investment(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_investment
    ADD CONSTRAINT fk_cb_investment_portfolio_id_portfolio FOREIGN KEY (portfolio_id) REFERENCES public.portfolio(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.cb_investment
    ADD CONSTRAINT fk_cb_investment_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_investment
    ADD CONSTRAINT fk_cb_investment_version_applied_id_cb_basket_version FOREIGN KEY (version_applied_id) REFERENCES public.cb_basket_version(id);

ALTER TABLE ONLY public.cb_manager_revenue_share
    ADD CONSTRAINT fk_cb_manager_revenue_share_manager_id_cb_manager FOREIGN KEY (manager_id) REFERENCES public.cb_manager(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_manager
    ADD CONSTRAINT fk_cb_manager_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id);

ALTER TABLE ONLY public.cb_metrics
    ADD CONSTRAINT fk_cb_metrics_basket_id_cb_basket FOREIGN KEY (basket_id) REFERENCES public.cb_basket(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_order_batch
    ADD CONSTRAINT fk_cb_order_batch_broker_account_id_broker_account FOREIGN KEY (broker_account_id) REFERENCES public.broker_account(id);

ALTER TABLE ONLY public.cb_order_batch
    ADD CONSTRAINT fk_cb_order_batch_fee_entry_id_cb_fee_ledger FOREIGN KEY (fee_entry_id) REFERENCES public.cb_fee_ledger(id);

ALTER TABLE ONLY public.cb_order_batch
    ADD CONSTRAINT fk_cb_order_batch_investment_id_cb_investment FOREIGN KEY (investment_id) REFERENCES public.cb_investment(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_order_batch
    ADD CONSTRAINT fk_cb_order_batch_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_pending_action
    ADD CONSTRAINT fk_cb_pending_action_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_plan
    ADD CONSTRAINT fk_cb_plan_basket_id_cb_basket FOREIGN KEY (basket_id) REFERENCES public.cb_basket(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.cb_plan
    ADD CONSTRAINT fk_cb_plan_manager_id_cb_manager FOREIGN KEY (manager_id) REFERENCES public.cb_manager(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.cb_sip_plan
    ADD CONSTRAINT fk_cb_sip_plan_investment_id_cb_investment FOREIGN KEY (investment_id) REFERENCES public.cb_investment(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_subscription
    ADD CONSTRAINT fk_cb_subscription_plan_id_cb_plan FOREIGN KEY (plan_id) REFERENCES public.cb_plan(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_subscription
    ADD CONSTRAINT fk_cb_subscription_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_update_post
    ADD CONSTRAINT fk_cb_update_post_basket_id_cb_basket FOREIGN KEY (basket_id) REFERENCES public.cb_basket(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.cb_update_post
    ADD CONSTRAINT fk_cb_update_post_manager_id_cb_manager FOREIGN KEY (manager_id) REFERENCES public.cb_manager(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.cb_user_rebalance_state
    ADD CONSTRAINT fk_cb_user_rebalance_state_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_user_rebalance_state
    ADD CONSTRAINT fk_cb_user_rebalance_state_version_id_cb_basket_version FOREIGN KEY (version_id) REFERENCES public.cb_basket_version(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_watchlist_item
    ADD CONSTRAINT fk_cb_watchlist_item_basket_id_cb_basket FOREIGN KEY (basket_id) REFERENCES public.cb_basket(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cb_watchlist_item
    ADD CONSTRAINT fk_cb_watchlist_item_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.consent_record
    ADD CONSTRAINT fk_consent_record_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.corporate_action
    ADD CONSTRAINT fk_corporate_action_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.desk_score_daily
    ADD CONSTRAINT fk_desk_score_daily_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.entitlement_override
    ADD CONSTRAINT fk_entitlement_override_granted_by_user_id_app_user FOREIGN KEY (granted_by_user_id) REFERENCES public.app_user(id);

ALTER TABLE ONLY public.entitlement_override
    ADD CONSTRAINT fk_entitlement_override_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.eq_minute_bar
    ADD CONSTRAINT fk_eq_minute_bar_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.factor_daily
    ADD CONSTRAINT fk_factor_daily_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.fo_backtest_run
    ADD CONSTRAINT fk_fo_backtest_run_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_book_config
    ADD CONSTRAINT fk_fo_book_config_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_config_audit
    ADD CONSTRAINT fk_fo_config_audit_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_fill
    ADD CONSTRAINT fk_fo_fill_leg_id_fo_leg FOREIGN KEY (leg_id) REFERENCES public.fo_leg(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_fill
    ADD CONSTRAINT fk_fo_fill_position_id_fo_position FOREIGN KEY (position_id) REFERENCES public.fo_position(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_fill
    ADD CONSTRAINT fk_fo_fill_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_journal
    ADD CONSTRAINT fk_fo_journal_position_id_fo_position FOREIGN KEY (position_id) REFERENCES public.fo_position(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_journal
    ADD CONSTRAINT fk_fo_journal_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_leg
    ADD CONSTRAINT fk_fo_leg_plan_id_fo_plan FOREIGN KEY (plan_id) REFERENCES public.fo_plan(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_leg
    ADD CONSTRAINT fk_fo_leg_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_mark
    ADD CONSTRAINT fk_fo_mark_position_id_fo_position FOREIGN KEY (position_id) REFERENCES public.fo_position(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_mark
    ADD CONSTRAINT fk_fo_mark_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_plan
    ADD CONSTRAINT fk_fo_plan_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_position
    ADD CONSTRAINT fk_fo_position_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_scan
    ADD CONSTRAINT fk_fo_scan_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fo_sleeve_config
    ADD CONSTRAINT fk_fo_sleeve_config_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.fundamental_daily
    ADD CONSTRAINT fk_fundamental_daily_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.index_bar_daily
    ADD CONSTRAINT fk_index_bar_daily_index_id_index_master FOREIGN KEY (index_id) REFERENCES public.index_master(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.index_constituents
    ADD CONSTRAINT fk_index_constituents_index_id_index_master FOREIGN KEY (index_id) REFERENCES public.index_master(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.index_member_daily
    ADD CONSTRAINT fk_index_member_daily_index_id_index_def FOREIGN KEY (index_id) REFERENCES public.index_def(id);

ALTER TABLE ONLY public.index_member_daily
    ADD CONSTRAINT fk_index_member_daily_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.index_snapshot_daily
    ADD CONSTRAINT fk_index_snapshot_daily_index_id_index_def FOREIGN KEY (index_id) REFERENCES public.index_def(id);

ALTER TABLE ONLY public.index_stats_daily
    ADD CONSTRAINT fk_index_stats_daily_index_id_index_master FOREIGN KEY (index_id) REFERENCES public.index_master(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.ingest_cursor
    ADD CONSTRAINT fk_ingest_cursor_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.instrument
    ADD CONSTRAINT fk_instrument_exchange_id_exchange FOREIGN KEY (exchange_id) REFERENCES public.exchange(id);

ALTER TABLE ONLY public.instrument_watch_item
    ADD CONSTRAINT fk_instrument_watch_item_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.instrument_watch_item
    ADD CONSTRAINT fk_instrument_watch_item_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.lv_adoption
    ADD CONSTRAINT fk_lv_adoption_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.lv_exit_order
    ADD CONSTRAINT fk_lv_exit_order_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.lv_heartbeat
    ADD CONSTRAINT fk_lv_heartbeat_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.lv_protection_issue
    ADD CONSTRAINT fk_lv_protection_issue_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.market_health_daily
    ADD CONSTRAINT fk_market_health_daily_index_id_index_def FOREIGN KEY (index_id) REFERENCES public.index_def(id);

ALTER TABLE ONLY public.ohlcv_daily
    ADD CONSTRAINT fk_ohlcv_daily_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.op_backtest_run
    ADD CONSTRAINT fk_op_backtest_run_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_book_config
    ADD CONSTRAINT fk_op_book_config_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_config_audit
    ADD CONSTRAINT fk_op_config_audit_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_event_day
    ADD CONSTRAINT fk_op_event_day_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_fill
    ADD CONSTRAINT fk_op_fill_leg_id_op_leg FOREIGN KEY (leg_id) REFERENCES public.op_leg(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_fill
    ADD CONSTRAINT fk_op_fill_order_id_op_order FOREIGN KEY (order_id) REFERENCES public.op_order(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_fill
    ADD CONSTRAINT fk_op_fill_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_index_minute
    ADD CONSTRAINT fk_op_index_minute_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.op_journal
    ADD CONSTRAINT fk_op_journal_session_id_op_session FOREIGN KEY (session_id) REFERENCES public.op_session(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_journal
    ADD CONSTRAINT fk_op_journal_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_leg
    ADD CONSTRAINT fk_op_leg_instrument_token_op_contract FOREIGN KEY (instrument_token) REFERENCES public.op_contract(instrument_token);

ALTER TABLE ONLY public.op_leg
    ADD CONSTRAINT fk_op_leg_plan_id_op_plan FOREIGN KEY (plan_id) REFERENCES public.op_plan(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_leg
    ADD CONSTRAINT fk_op_leg_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_order
    ADD CONSTRAINT fk_op_order_leg_id_op_leg FOREIGN KEY (leg_id) REFERENCES public.op_leg(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_order
    ADD CONSTRAINT fk_op_order_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_plan
    ADD CONSTRAINT fk_op_plan_session_id_op_session FOREIGN KEY (session_id) REFERENCES public.op_session(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_plan
    ADD CONSTRAINT fk_op_plan_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_position
    ADD CONSTRAINT fk_op_position_session_id_op_session FOREIGN KEY (session_id) REFERENCES public.op_session(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_position
    ADD CONSTRAINT fk_op_position_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_scan
    ADD CONSTRAINT fk_op_scan_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_session
    ADD CONSTRAINT fk_op_session_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.op_sleeve_config
    ADD CONSTRAINT fk_op_sleeve_config_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT fk_payment_subscription_id_subscription FOREIGN KEY (subscription_id) REFERENCES public.subscription(id);

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT fk_payment_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id);

ALTER TABLE ONLY public.pipeline_run_step
    ADD CONSTRAINT fk_pipeline_run_step_run_id_pipeline_run FOREIGN KEY (run_id) REFERENCES public.pipeline_run(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.portfolio
    ADD CONSTRAINT fk_portfolio_benchmark_index_id_index_def FOREIGN KEY (benchmark_index_id) REFERENCES public.index_def(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.portfolio
    ADD CONSTRAINT fk_portfolio_broker_account_id_broker_account FOREIGN KEY (broker_account_id) REFERENCES public.broker_account(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.portfolio_cash_flow
    ADD CONSTRAINT fk_portfolio_cash_flow_broker_account_id_broker_account FOREIGN KEY (broker_account_id) REFERENCES public.broker_account(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.portfolio_cash_flow
    ADD CONSTRAINT fk_portfolio_cash_flow_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.portfolio_cash_flow
    ADD CONSTRAINT fk_portfolio_cash_flow_portfolio_id_portfolio FOREIGN KEY (portfolio_id) REFERENCES public.portfolio(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.portfolio_holding
    ADD CONSTRAINT fk_portfolio_holding_broker_account_id_broker_account FOREIGN KEY (broker_account_id) REFERENCES public.broker_account(id);

ALTER TABLE ONLY public.portfolio_holding
    ADD CONSTRAINT fk_portfolio_holding_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.portfolio_holding
    ADD CONSTRAINT fk_portfolio_holding_portfolio_id_portfolio FOREIGN KEY (portfolio_id) REFERENCES public.portfolio(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.portfolio_holding
    ADD CONSTRAINT fk_portfolio_holding_portfolio_kind FOREIGN KEY (portfolio_id, portfolio_kind) REFERENCES public.portfolio(id, kind) ON UPDATE CASCADE;

ALTER TABLE ONLY public.portfolio_nav_daily
    ADD CONSTRAINT fk_portfolio_nav_daily_portfolio_id_portfolio FOREIGN KEY (portfolio_id) REFERENCES public.portfolio(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.portfolio_nav_daily
    ADD CONSTRAINT fk_portfolio_nav_daily_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.portfolio
    ADD CONSTRAINT fk_portfolio_parent_id_portfolio FOREIGN KEY (parent_id) REFERENCES public.portfolio(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.portfolio_rebalance
    ADD CONSTRAINT fk_portfolio_rebalance_portfolio_id_portfolio FOREIGN KEY (portfolio_id) REFERENCES public.portfolio(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.portfolio_rebalance
    ADD CONSTRAINT fk_portfolio_rebalance_screen_id_screen FOREIGN KEY (screen_id) REFERENCES public.screen(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.portfolio_sleeve
    ADD CONSTRAINT fk_portfolio_sleeve_basket_id_cb_basket FOREIGN KEY (basket_id) REFERENCES public.cb_basket(id);

ALTER TABLE ONLY public.portfolio_sleeve
    ADD CONSTRAINT fk_portfolio_sleeve_portfolio_id_portfolio FOREIGN KEY (portfolio_id) REFERENCES public.portfolio(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.portfolio_sleeve
    ADD CONSTRAINT fk_portfolio_sleeve_screen_id_screen FOREIGN KEY (screen_id) REFERENCES public.screen(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.portfolio
    ADD CONSTRAINT fk_portfolio_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id);

ALTER TABLE ONLY public.reconciliation_item
    ADD CONSTRAINT fk_reconciliation_item_broker_account_id_broker_account FOREIGN KEY (broker_account_id) REFERENCES public.broker_account(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.reconciliation_item
    ADD CONSTRAINT fk_reconciliation_item_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.reconciliation_item
    ADD CONSTRAINT fk_reconciliation_item_resolved_portfolio_id_portfolio FOREIGN KEY (resolved_portfolio_id) REFERENCES public.portfolio(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.reconciliation_item
    ADD CONSTRAINT fk_reconciliation_item_suggested_portfolio_id_portfolio FOREIGN KEY (suggested_portfolio_id) REFERENCES public.portfolio(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.reconciliation_item
    ADD CONSTRAINT fk_reconciliation_item_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.refresh_token
    ADD CONSTRAINT fk_refresh_token_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.risk_ledger
    ADD CONSTRAINT fk_risk_ledger_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.screen_alert_delivery
    ADD CONSTRAINT fk_screen_alert_delivery_alert_id_screen_alert FOREIGN KEY (alert_id) REFERENCES public.screen_alert(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.screen_alert
    ADD CONSTRAINT fk_screen_alert_last_run_id_screen_run FOREIGN KEY (last_run_id) REFERENCES public.screen_run(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.screen_alert
    ADD CONSTRAINT fk_screen_alert_screen_id_screen FOREIGN KEY (screen_id) REFERENCES public.screen(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.screen_alert
    ADD CONSTRAINT fk_screen_alert_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.screen_run
    ADD CONSTRAINT fk_screen_run_screen_id_screen FOREIGN KEY (screen_id) REFERENCES public.screen(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.screen
    ADD CONSTRAINT fk_screen_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id);

ALTER TABLE ONLY public.subscription
    ADD CONSTRAINT fk_subscription_plan_id_plan FOREIGN KEY (plan_id) REFERENCES public.plan(id);

ALTER TABLE ONLY public.subscription
    ADD CONSTRAINT fk_subscription_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id);

ALTER TABLE ONLY public.sw_backtest_run
    ADD CONSTRAINT fk_sw_backtest_run_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_catalyst
    ADD CONSTRAINT fk_sw_catalyst_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_catalyst
    ADD CONSTRAINT fk_sw_catalyst_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_config_audit
    ADD CONSTRAINT fk_sw_config_audit_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_config
    ADD CONSTRAINT fk_sw_config_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_fill
    ADD CONSTRAINT fk_sw_fill_position_id_sw_position FOREIGN KEY (position_id) REFERENCES public.sw_position(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_fill
    ADD CONSTRAINT fk_sw_fill_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_market_daily
    ADD CONSTRAINT fk_sw_market_daily_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_plan_line
    ADD CONSTRAINT fk_sw_plan_line_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.sw_plan_line
    ADD CONSTRAINT fk_sw_plan_line_plan_id_sw_plan FOREIGN KEY (plan_id) REFERENCES public.sw_plan(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_plan_line
    ADD CONSTRAINT fk_sw_plan_line_position_id_sw_position FOREIGN KEY (position_id) REFERENCES public.sw_position(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.sw_plan_line
    ADD CONSTRAINT fk_sw_plan_line_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_plan_skip
    ADD CONSTRAINT fk_sw_plan_skip_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.sw_plan_skip
    ADD CONSTRAINT fk_sw_plan_skip_plan_id_sw_plan FOREIGN KEY (plan_id) REFERENCES public.sw_plan(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_plan_skip
    ADD CONSTRAINT fk_sw_plan_skip_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_plan
    ADD CONSTRAINT fk_sw_plan_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_position
    ADD CONSTRAINT fk_sw_position_broker_account_id_broker_account FOREIGN KEY (broker_account_id) REFERENCES public.broker_account(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.sw_position
    ADD CONSTRAINT fk_sw_position_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.sw_position
    ADD CONSTRAINT fk_sw_position_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_scan_run
    ADD CONSTRAINT fk_sw_scan_run_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_session
    ADD CONSTRAINT fk_sw_session_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_setup_daily
    ADD CONSTRAINT fk_sw_setup_daily_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.sw_setup_daily
    ADD CONSTRAINT fk_sw_setup_daily_pipeline_run_id_pipeline_run FOREIGN KEY (pipeline_run_id) REFERENCES public.pipeline_run(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.sw_setup_daily
    ADD CONSTRAINT fk_sw_setup_daily_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_signal
    ADD CONSTRAINT fk_sw_signal_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.sw_signal
    ADD CONSTRAINT fk_sw_signal_plan_line_id_sw_plan_line FOREIGN KEY (plan_line_id) REFERENCES public.sw_plan_line(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.sw_signal
    ADD CONSTRAINT fk_sw_signal_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sw_signal
    ADD CONSTRAINT fk_sw_signal_watch_id_sw_watch FOREIGN KEY (watch_id) REFERENCES public.sw_watch(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.sw_watch
    ADD CONSTRAINT fk_sw_watch_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.sw_watch
    ADD CONSTRAINT fk_sw_watch_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.swing_candidates
    ADD CONSTRAINT fk_swing_candidates_run_id_swing_regime_run FOREIGN KEY (run_id) REFERENCES public.swing_regime_run(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.symbol_alias
    ADD CONSTRAINT fk_symbol_alias_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);

ALTER TABLE ONLY public.trading_day
    ADD CONSTRAINT fk_trading_day_exchange_id_exchange FOREIGN KEY (exchange_id) REFERENCES public.exchange(id);

ALTER TABLE ONLY public.tw_backtest_run
    ADD CONSTRAINT fk_tw_backtest_run_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_breadth_daily
    ADD CONSTRAINT fk_tw_breadth_daily_pipeline_run_id_pipeline_run FOREIGN KEY (pipeline_run_id) REFERENCES public.pipeline_run(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.tw_breadth_daily
    ADD CONSTRAINT fk_tw_breadth_daily_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_config_audit
    ADD CONSTRAINT fk_tw_config_audit_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_config
    ADD CONSTRAINT fk_tw_config_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_fill
    ADD CONSTRAINT fk_tw_fill_order_id_tw_order FOREIGN KEY (order_id) REFERENCES public.tw_order(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.tw_fill
    ADD CONSTRAINT fk_tw_fill_position_id_tw_position FOREIGN KEY (position_id) REFERENCES public.tw_position(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_fill
    ADD CONSTRAINT fk_tw_fill_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_order
    ADD CONSTRAINT fk_tw_order_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_order
    ADD CONSTRAINT fk_tw_order_position_id_tw_position FOREIGN KEY (position_id) REFERENCES public.tw_position(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.tw_order
    ADD CONSTRAINT fk_tw_order_signal FOREIGN KEY (user_id, signal_date, instrument_id) REFERENCES public.tw_signal_daily(user_id, date, instrument_id);

ALTER TABLE ONLY public.tw_order
    ADD CONSTRAINT fk_tw_order_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_plan_line
    ADD CONSTRAINT fk_tw_plan_line_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_plan_line
    ADD CONSTRAINT fk_tw_plan_line_order_id_tw_order FOREIGN KEY (order_id) REFERENCES public.tw_order(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.tw_plan_line
    ADD CONSTRAINT fk_tw_plan_line_plan_id_tw_plan FOREIGN KEY (plan_id) REFERENCES public.tw_plan(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_plan_line
    ADD CONSTRAINT fk_tw_plan_line_position_id_tw_position FOREIGN KEY (position_id) REFERENCES public.tw_position(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.tw_plan_line
    ADD CONSTRAINT fk_tw_plan_line_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_plan_skip
    ADD CONSTRAINT fk_tw_plan_skip_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_plan_skip
    ADD CONSTRAINT fk_tw_plan_skip_plan_id_tw_plan FOREIGN KEY (plan_id) REFERENCES public.tw_plan(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_plan_skip
    ADD CONSTRAINT fk_tw_plan_skip_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_plan
    ADD CONSTRAINT fk_tw_plan_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_position
    ADD CONSTRAINT fk_tw_position_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_position
    ADD CONSTRAINT fk_tw_position_order_id_tw_order FOREIGN KEY (order_id) REFERENCES public.tw_order(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.tw_position
    ADD CONSTRAINT fk_tw_position_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_scan_run
    ADD CONSTRAINT fk_tw_scan_run_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_session
    ADD CONSTRAINT fk_tw_session_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_signal_daily
    ADD CONSTRAINT fk_tw_signal_daily_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_signal_daily
    ADD CONSTRAINT fk_tw_signal_daily_pipeline_run_id_pipeline_run FOREIGN KEY (pipeline_run_id) REFERENCES public.pipeline_run(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.tw_signal_daily
    ADD CONSTRAINT fk_tw_signal_daily_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_state_daily
    ADD CONSTRAINT fk_tw_state_daily_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tw_state_daily
    ADD CONSTRAINT fk_tw_state_daily_pipeline_run_id_pipeline_run FOREIGN KEY (pipeline_run_id) REFERENCES public.pipeline_run(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.tw_state_daily
    ADD CONSTRAINT fk_tw_state_daily_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_discover_preferences
    ADD CONSTRAINT fk_user_discover_preferences_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_backtest_run
    ADD CONSTRAINT fk_vb_backtest_run_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_breadth_daily
    ADD CONSTRAINT fk_vb_breadth_daily_pipeline_run_id_pipeline_run FOREIGN KEY (pipeline_run_id) REFERENCES public.pipeline_run(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.vb_breadth_daily
    ADD CONSTRAINT fk_vb_breadth_daily_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_config_audit
    ADD CONSTRAINT fk_vb_config_audit_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_config
    ADD CONSTRAINT fk_vb_config_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_fill
    ADD CONSTRAINT fk_vb_fill_order_id_vb_order FOREIGN KEY (order_id) REFERENCES public.vb_order(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.vb_fill
    ADD CONSTRAINT fk_vb_fill_position_id_vb_position FOREIGN KEY (position_id) REFERENCES public.vb_position(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_fill
    ADD CONSTRAINT fk_vb_fill_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_order
    ADD CONSTRAINT fk_vb_order_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_order
    ADD CONSTRAINT fk_vb_order_position_id_vb_position FOREIGN KEY (position_id) REFERENCES public.vb_position(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.vb_order
    ADD CONSTRAINT fk_vb_order_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_plan_line
    ADD CONSTRAINT fk_vb_plan_line_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_plan_line
    ADD CONSTRAINT fk_vb_plan_line_order_id_vb_order FOREIGN KEY (order_id) REFERENCES public.vb_order(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.vb_plan_line
    ADD CONSTRAINT fk_vb_plan_line_plan_id_vb_plan FOREIGN KEY (plan_id) REFERENCES public.vb_plan(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_plan_line
    ADD CONSTRAINT fk_vb_plan_line_position_id_vb_position FOREIGN KEY (position_id) REFERENCES public.vb_position(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.vb_plan_line
    ADD CONSTRAINT fk_vb_plan_line_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_plan_skip
    ADD CONSTRAINT fk_vb_plan_skip_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_plan_skip
    ADD CONSTRAINT fk_vb_plan_skip_plan_id_vb_plan FOREIGN KEY (plan_id) REFERENCES public.vb_plan(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_plan_skip
    ADD CONSTRAINT fk_vb_plan_skip_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_plan
    ADD CONSTRAINT fk_vb_plan_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_position
    ADD CONSTRAINT fk_vb_position_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_position
    ADD CONSTRAINT fk_vb_position_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_scan_run
    ADD CONSTRAINT fk_vb_scan_run_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_session
    ADD CONSTRAINT fk_vb_session_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_signal_daily
    ADD CONSTRAINT fk_vb_signal_daily_instrument_id_instrument FOREIGN KEY (instrument_id) REFERENCES public.instrument(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.vb_signal_daily
    ADD CONSTRAINT fk_vb_signal_daily_pipeline_run_id_pipeline_run FOREIGN KEY (pipeline_run_id) REFERENCES public.pipeline_run(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.vb_signal_daily
    ADD CONSTRAINT fk_vb_signal_daily_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.webhook_delivery
    ADD CONSTRAINT fk_webhook_delivery_endpoint_id_webhook_endpoint FOREIGN KEY (endpoint_id) REFERENCES public.webhook_endpoint(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.webhook_endpoint
    ADD CONSTRAINT fk_webhook_endpoint_screen_id_screen FOREIGN KEY (screen_id) REFERENCES public.screen(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.webhook_endpoint
    ADD CONSTRAINT fk_webhook_endpoint_user_id_app_user FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;

INSERT INTO public.index_master VALUES (1, 'Nifty 50', 'Broad market', NULL, NULL, 'ind_nifty50list', false, NULL, 1);
INSERT INTO public.index_master VALUES (2, 'Nifty Next 50', 'Broad market', NULL, NULL, 'ind_niftynext50list', false, NULL, 2);
INSERT INTO public.index_master VALUES (3, 'Nifty 100', 'Broad market', NULL, NULL, 'ind_nifty100list', false, NULL, 3);
INSERT INTO public.index_master VALUES (4, 'Nifty 200', 'Broad market', NULL, NULL, 'ind_nifty200list', false, NULL, 4);
INSERT INTO public.index_master VALUES (5, 'Nifty 500', 'Broad market', NULL, NULL, 'ind_nifty500list', false, NULL, 5);
INSERT INTO public.index_master VALUES (6, 'Nifty Midcap 150', 'Broad market', NULL, NULL, 'ind_niftymidcap150list', false, NULL, 6);
INSERT INTO public.index_master VALUES (7, 'Nifty Midcap 50', 'Broad market', NULL, NULL, 'ind_niftymidcap50list', false, NULL, 7);
INSERT INTO public.index_master VALUES (8, 'Nifty Midcap Select', 'Broad market', NULL, NULL, 'ind_niftymidcapselectlist', false, NULL, 8);
INSERT INTO public.index_master VALUES (9, 'Nifty Midcap 100', 'Broad market', NULL, NULL, 'ind_niftymidcap100list', false, NULL, 9);
INSERT INTO public.index_master VALUES (10, 'Nifty Smallcap 250', 'Broad market', NULL, NULL, 'ind_niftysmallcap250list', false, NULL, 10);
INSERT INTO public.index_master VALUES (11, 'Nifty Smallcap 50', 'Broad market', NULL, NULL, 'ind_niftysmallcap50list', false, NULL, 11);
INSERT INTO public.index_master VALUES (12, 'Nifty Smallcap 100', 'Broad market', NULL, NULL, 'ind_niftysmallcap100list', false, NULL, 12);
INSERT INTO public.index_master VALUES (13, 'Nifty LargeMidcap 250', 'Broad market', NULL, NULL, 'ind_niftylargemidcap250list', false, NULL, 13);
INSERT INTO public.index_master VALUES (14, 'Nifty MidSmallcap 400', 'Broad market', NULL, NULL, 'ind_niftymidsmallcap400list', false, NULL, 14);
INSERT INTO public.index_master VALUES (15, 'Nifty500 Multicap 50:25:25', 'Broad market', NULL, NULL, 'ind_nifty500multicap502525list', false, NULL, 15);
INSERT INTO public.index_master VALUES (16, 'Nifty Microcap 250', 'Broad market', NULL, NULL, 'ind_niftymicrocap250list', false, NULL, 16);
INSERT INTO public.index_master VALUES (17, 'Nifty Total Market', 'Broad market', NULL, NULL, 'ind_niftytotalmarketlist', false, NULL, 17);
INSERT INTO public.index_master VALUES (18, 'Nifty500 LargeMidSmall Equal-Cap Weighted', 'Broad market', NULL, NULL, 'ind_nifty500largemidsmallequalcapweightedlist', false, NULL, 18);
INSERT INTO public.index_master VALUES (19, 'Nifty India FPI 150', 'Broad market', NULL, NULL, 'ind_niftyindiafpi150list', false, NULL, 19);
INSERT INTO public.index_master VALUES (20, 'Nifty Smallcap 500', 'Broad market', NULL, NULL, 'ind_niftysmallcap500list', false, NULL, 20);
INSERT INTO public.index_master VALUES (21, 'Nifty MidSmallcap400 50:50', 'Broad market', NULL, NULL, 'ind_niftymidsmallcap4005050list', false, NULL, 21);
INSERT INTO public.index_master VALUES (22, 'Nifty Next 100', 'Broad market', NULL, NULL, 'ind_niftynext100list', false, NULL, 22);
INSERT INTO public.index_master VALUES (23, 'Nifty Auto', 'Sectoral', NULL, NULL, 'ind_niftyautolist', false, NULL, 23);
INSERT INTO public.index_master VALUES (24, 'Nifty Bank', 'Sectoral', NULL, NULL, 'ind_niftybanklist', false, NULL, 24);
INSERT INTO public.index_master VALUES (25, 'Nifty Financial Services', 'Sectoral', NULL, NULL, 'ind_niftyfinancialserviceslist', false, NULL, 25);
INSERT INTO public.index_master VALUES (26, 'Nifty FMCG', 'Sectoral', NULL, NULL, 'ind_niftyfmcglist', false, NULL, 26);
INSERT INTO public.index_master VALUES (27, 'Nifty IT', 'Sectoral', NULL, NULL, 'ind_niftyitlist', false, NULL, 27);
INSERT INTO public.index_master VALUES (28, 'Nifty Media', 'Sectoral', NULL, NULL, 'ind_niftymedialist', false, NULL, 28);
INSERT INTO public.index_master VALUES (29, 'Nifty Metal', 'Sectoral', NULL, NULL, 'ind_niftymetallist', false, NULL, 29);
INSERT INTO public.index_master VALUES (30, 'Nifty Pharma', 'Sectoral', NULL, NULL, 'ind_niftypharmalist', false, NULL, 30);
INSERT INTO public.index_master VALUES (31, 'Nifty Private Bank', 'Sectoral', NULL, NULL, 'ind_niftyprivatebanklist', false, NULL, 31);
INSERT INTO public.index_master VALUES (32, 'Nifty PSU Bank', 'Sectoral', NULL, NULL, 'ind_niftypsubanklist', false, NULL, 32);
INSERT INTO public.index_master VALUES (33, 'Nifty Realty', 'Sectoral', NULL, NULL, 'ind_niftyrealtylist', false, NULL, 33);
INSERT INTO public.index_master VALUES (34, 'Nifty Consumer Durables', 'Sectoral', NULL, NULL, 'ind_niftyconsumerdurableslist', false, NULL, 34);
INSERT INTO public.index_master VALUES (35, 'Nifty Oil & Gas', 'Sectoral', NULL, NULL, 'ind_niftyoilgaslist', false, NULL, 35);
INSERT INTO public.index_master VALUES (36, 'Nifty Financial Services 25/50', 'Sectoral', NULL, NULL, 'ind_niftyfinancialservices2550list', false, NULL, 36);
INSERT INTO public.index_master VALUES (37, 'Nifty Healthcare', 'Sectoral', NULL, NULL, 'ind_niftyhealthcarelist', false, NULL, 37);
INSERT INTO public.index_master VALUES (38, 'Nifty Financial Services Ex-Bank', 'Sectoral', NULL, NULL, 'ind_niftyfinancialservicesexbanklist', false, NULL, 38);
INSERT INTO public.index_master VALUES (39, 'Nifty MidSmall Financial Services', 'Sectoral', NULL, NULL, 'ind_niftymidsmallfinancialserviceslist', false, NULL, 39);
INSERT INTO public.index_master VALUES (40, 'Nifty MidSmall Healthcare', 'Sectoral', NULL, NULL, 'ind_niftymidsmallhealthcarelist', false, NULL, 40);
INSERT INTO public.index_master VALUES (41, 'Nifty MidSmall IT & Telecom', 'Sectoral', NULL, NULL, 'ind_niftymidsmallittelecomlist', false, NULL, 41);
INSERT INTO public.index_master VALUES (42, 'Nifty Chemicals', 'Sectoral', NULL, NULL, 'ind_niftychemicalslist', false, NULL, 42);
INSERT INTO public.index_master VALUES (43, 'Nifty500 Healthcare', 'Sectoral', NULL, NULL, 'ind_nifty500healthcarelist', false, NULL, 43);
INSERT INTO public.index_master VALUES (44, 'Nifty Cement', 'Sectoral', NULL, NULL, 'ind_niftycementlist', false, NULL, 44);
INSERT INTO public.index_master VALUES (45, 'Nifty Telecommunications', 'Sectoral', NULL, NULL, 'ind_niftytelecommunicationslist', false, NULL, 45);
INSERT INTO public.index_master VALUES (46, 'Nifty Retail', 'Sectoral', NULL, NULL, 'ind_niftyretaillist', false, NULL, 46);
INSERT INTO public.index_master VALUES (47, 'Nifty Power', 'Sectoral', NULL, NULL, 'ind_niftypowerlist', false, NULL, 47);
INSERT INTO public.index_master VALUES (48, 'Nifty NBFC', 'Sectoral', NULL, NULL, 'ind_niftynbfclist', false, NULL, 48);
INSERT INTO public.index_master VALUES (49, 'Nifty Insurance', 'Sectoral', NULL, NULL, 'ind_niftyinsurancelist', false, NULL, 49);
INSERT INTO public.index_master VALUES (50, 'Nifty Housing Finance', 'Sectoral', NULL, NULL, 'ind_niftyhousingfinancelist', false, NULL, 50);
INSERT INTO public.index_master VALUES (51, 'Nifty Hospitals', 'Sectoral', NULL, NULL, 'ind_niftyhospitalslist', false, NULL, 51);
INSERT INTO public.index_master VALUES (52, 'Nifty Consumer Services', 'Sectoral', NULL, NULL, 'ind_niftyconsumerserviceslist', false, NULL, 52);
INSERT INTO public.index_master VALUES (53, 'Nifty Construction', 'Sectoral', NULL, NULL, 'ind_niftyconstructionlist', false, NULL, 53);
INSERT INTO public.index_master VALUES (54, 'Nifty Commercial & Transport Services', 'Sectoral', NULL, NULL, 'ind_niftycommercialtransportserviceslist', false, NULL, 54);
INSERT INTO public.index_master VALUES (55, 'Nifty Capital Goods', 'Sectoral', NULL, NULL, 'ind_niftycapitalgoodslist', false, NULL, 55);
INSERT INTO public.index_master VALUES (56, 'Nifty REITs & Realty', 'Sectoral', NULL, NULL, 'ind_niftyreitsrealtylist', false, NULL, 56);
INSERT INTO public.index_master VALUES (57, 'Nifty India Corporate Group Index - Aditya Birla Group', 'Thematic', NULL, NULL, 'ind_niftyindiacorporategroupindexadityabirlagrouplist', false, NULL, 57);
INSERT INTO public.index_master VALUES (58, 'Nifty Commodities', 'Thematic', NULL, NULL, 'ind_niftycommoditieslist', false, NULL, 58);
INSERT INTO public.index_master VALUES (59, 'Nifty CPSE', 'Thematic', NULL, NULL, 'ind_niftycpselist', false, NULL, 59);
INSERT INTO public.index_master VALUES (60, 'Nifty Energy', 'Thematic', NULL, NULL, 'ind_niftyenergylist', false, NULL, 60);
INSERT INTO public.index_master VALUES (61, 'Nifty India Consumption', 'Thematic', NULL, NULL, 'ind_niftyindiaconsumptionlist', false, NULL, 61);
INSERT INTO public.index_master VALUES (62, 'Nifty Infrastructure', 'Thematic', NULL, NULL, 'ind_niftyinfrastructurelist', false, NULL, 62);
INSERT INTO public.index_master VALUES (63, 'Nifty India Manufacturing', 'Thematic', NULL, NULL, 'ind_niftyindiamanufacturinglist', false, NULL, 63);
INSERT INTO public.index_master VALUES (64, 'Nifty India Corporate Group Index - Mahindra Group', 'Thematic', NULL, NULL, 'ind_niftyindiacorporategroupindexmahindragrouplist', false, NULL, 64);
INSERT INTO public.index_master VALUES (65, 'Nifty Midcap Liquid 15', 'Thematic', NULL, NULL, 'ind_niftymidcapliquid15list', false, NULL, 65);
INSERT INTO public.index_master VALUES (66, 'Nifty MNC', 'Thematic', NULL, NULL, 'ind_niftymnclist', false, NULL, 66);
INSERT INTO public.index_master VALUES (67, 'Nifty PSE', 'Thematic', NULL, NULL, 'ind_niftypselist', false, NULL, 67);
INSERT INTO public.index_master VALUES (68, 'Nifty Services Sector', 'Thematic', NULL, NULL, 'ind_niftyservicessectorlist', false, NULL, 68);
INSERT INTO public.index_master VALUES (69, 'Nifty Shariah 25', 'Thematic', NULL, NULL, 'ind_niftyshariah25list', false, NULL, 69);
INSERT INTO public.index_master VALUES (70, 'Nifty India Corporate Group Index - Tata Group', 'Thematic', NULL, NULL, 'ind_niftyindiacorporategroupindextatagrouplist', false, NULL, 70);
INSERT INTO public.index_master VALUES (71, 'Nifty100 Liquid 15', 'Thematic', NULL, NULL, 'ind_nifty100liquid15list', false, NULL, 71);
INSERT INTO public.index_master VALUES (72, 'Nifty India Corporate Group Index - Tata Group 25% Cap', 'Thematic', NULL, NULL, 'ind_niftyindiacorporategroupindextatagroup25caplist', false, NULL, 72);
INSERT INTO public.index_master VALUES (73, 'Nifty50 Shariah', 'Thematic', NULL, NULL, 'ind_nifty50shariahlist', false, NULL, 73);
INSERT INTO public.index_master VALUES (74, 'Nifty500 Shariah', 'Thematic', NULL, NULL, 'ind_nifty500shariahlist', false, NULL, 74);
INSERT INTO public.index_master VALUES (75, 'Nifty SME EMERGE', 'Thematic', NULL, NULL, 'ind_niftysmeemergelist', false, NULL, 75);
INSERT INTO public.index_master VALUES (76, 'Nifty100 Enhanced ESG', 'Thematic', NULL, NULL, 'ind_nifty100enhancedesglist', false, NULL, 76);
INSERT INTO public.index_master VALUES (77, 'Nifty100 ESG', 'Thematic', NULL, NULL, 'ind_nifty100esglist', false, NULL, 77);
INSERT INTO public.index_master VALUES (78, 'Nifty100 ESG Sector Leaders', 'Thematic', NULL, NULL, 'ind_nifty100esgsectorleaderslist', false, NULL, 78);
INSERT INTO public.index_master VALUES (79, 'Nifty Mobility', 'Thematic', NULL, NULL, 'ind_niftymobilitylist', false, NULL, 79);
INSERT INTO public.index_master VALUES (80, 'Nifty Non-Cyclical Consumer', 'Thematic', NULL, NULL, 'ind_niftynoncyclicalconsumerlist', false, NULL, 80);
INSERT INTO public.index_master VALUES (81, 'Nifty India Digital', 'Thematic', NULL, NULL, 'ind_niftyindiadigitallist', false, NULL, 81);
INSERT INTO public.index_master VALUES (82, 'Nifty India Defence', 'Thematic', NULL, NULL, 'ind_niftyindiadefencelist', false, NULL, 82);
INSERT INTO public.index_master VALUES (83, 'Nifty Housing', 'Thematic', NULL, NULL, 'ind_niftyhousinglist', false, NULL, 83);
INSERT INTO public.index_master VALUES (84, 'Nifty Transportation & Logistics', 'Thematic', NULL, NULL, 'ind_niftytransportationlogisticslist', false, NULL, 84);
INSERT INTO public.index_master VALUES (85, 'Nifty MidSmall India Consumption', 'Thematic', NULL, NULL, 'ind_niftymidsmallindiaconsumptionlist', false, NULL, 85);
INSERT INTO public.index_master VALUES (86, 'Nifty REITs & InvITs', 'Thematic', NULL, NULL, 'ind_niftyreitsinvitslist', false, NULL, 86);
INSERT INTO public.index_master VALUES (87, 'Nifty Core Housing', 'Thematic', NULL, NULL, 'ind_niftycorehousinglist', false, NULL, 87);
INSERT INTO public.index_master VALUES (88, 'Nifty500 Multicap India Manufacturing 50:30:20', 'Thematic', NULL, NULL, 'ind_nifty500multicapindiamanufacturing503020list', false, NULL, 88);
INSERT INTO public.index_master VALUES (89, 'Nifty500 Multicap Infrastructure 50:30:20', 'Thematic', NULL, NULL, 'ind_nifty500multicapinfrastructure503020list', false, NULL, 89);
INSERT INTO public.index_master VALUES (90, 'Nifty EV & New Age Automotive', 'Thematic', NULL, NULL, 'ind_niftyevnewageautomotivelist', false, NULL, 90);
INSERT INTO public.index_master VALUES (91, 'Nifty India Tourism', 'Thematic', NULL, NULL, 'ind_niftyindiatourismlist', false, NULL, 91);
INSERT INTO public.index_master VALUES (92, 'Nifty IPO', 'Thematic', NULL, NULL, 'ind_niftyipolist', false, NULL, 92);
INSERT INTO public.index_master VALUES (93, 'Nifty Rural', 'Thematic', NULL, NULL, 'ind_niftyrurallist', false, NULL, 93);
INSERT INTO public.index_master VALUES (94, 'Nifty Capital Markets', 'Thematic', NULL, NULL, 'ind_niftycapitalmarketslist', false, NULL, 94);
INSERT INTO public.index_master VALUES (95, 'Nifty India New Age Consumption', 'Thematic', NULL, NULL, 'ind_niftyindianewageconsumptionlist', false, NULL, 95);
INSERT INTO public.index_master VALUES (96, 'Nifty India Select 5 Corporate Groups (MAATR)', 'Thematic', NULL, NULL, 'ind_niftyindiaselect5corporategroupsmaatrlist', false, NULL, 96);
INSERT INTO public.index_master VALUES (97, 'Nifty India Railways PSU', 'Thematic', NULL, NULL, 'ind_niftyindiarailwayspsulist', false, NULL, 97);
INSERT INTO public.index_master VALUES (98, 'Nifty India Internet', 'Thematic', NULL, NULL, 'ind_niftyindiainternetlist', false, NULL, 98);
INSERT INTO public.index_master VALUES (99, 'Nifty Waves', 'Thematic', NULL, NULL, 'ind_niftywaveslist', false, NULL, 99);
INSERT INTO public.index_master VALUES (100, 'Nifty India Infrastructure & Logistics', 'Thematic', NULL, NULL, 'ind_niftyindiainfrastructurelogisticslist', false, NULL, 100);
INSERT INTO public.index_master VALUES (101, 'Nifty Conglomerate 50', 'Thematic', NULL, NULL, 'ind_niftyconglomerate50list', false, NULL, 101);
INSERT INTO public.index_master VALUES (102, 'Nifty Small Finance Banks & Microfinance Institutions', 'Thematic', NULL, NULL, 'ind_niftysmallfinancebanksmicrofinanceinstitutionslist', false, NULL, 102);
INSERT INTO public.index_master VALUES (103, 'Nifty Sugar & Ethanol', 'Thematic', NULL, NULL, 'ind_niftysugarethanollist', false, NULL, 103);
INSERT INTO public.index_master VALUES (104, 'Nifty500 Ahimsa', 'Thematic', NULL, NULL, 'ind_nifty500ahimsalist', false, NULL, 104);
INSERT INTO public.index_master VALUES (105, 'Nifty REITs & InvITs 90:10', 'Thematic', NULL, NULL, 'ind_niftyreitsinvits9010list', false, NULL, 105);
INSERT INTO public.index_master VALUES (106, 'Nifty India Defence Equal Weight', 'Thematic', NULL, NULL, 'ind_niftyindiadefenceequalweightlist', false, NULL, 106);
INSERT INTO public.index_master VALUES (107, 'Nifty AI Catalysts', 'Thematic', NULL, NULL, 'ind_niftyaicatalystslist', false, NULL, 107);
INSERT INTO public.index_master VALUES (108, 'Nifty 100 Equal Weight', 'Strategy', NULL, NULL, 'ind_nifty100equalweightlist', false, NULL, 108);
INSERT INTO public.index_master VALUES (109, 'Nifty100 Low Volatility 30', 'Strategy', NULL, NULL, 'ind_nifty100lowvolatility30list', false, NULL, 109);
INSERT INTO public.index_master VALUES (110, 'Nifty Alpha 50', 'Strategy', NULL, NULL, 'ind_niftyalpha50list', false, NULL, 110);
INSERT INTO public.index_master VALUES (111, 'Nifty 50 Arbitrage', 'Strategy', NULL, NULL, 'ind_nifty50arbitragelist', false, NULL, 111);
INSERT INTO public.index_master VALUES (112, 'Nifty 50 Futures PR', 'Strategy', NULL, NULL, 'ind_nifty50futuresprlist', false, NULL, 112);
INSERT INTO public.index_master VALUES (113, 'Nifty 50 Futures TR', 'Strategy', NULL, NULL, 'ind_nifty50futurestrlist', false, NULL, 113);
INSERT INTO public.index_master VALUES (114, 'Nifty Dividend Opportunities 50', 'Strategy', NULL, NULL, 'ind_niftydividendopportunities50list', false, NULL, 114);
INSERT INTO public.index_master VALUES (115, 'Nifty High Beta 50', 'Strategy', NULL, NULL, 'ind_niftyhighbeta50list', false, NULL, 115);
INSERT INTO public.index_master VALUES (116, 'Nifty Low Volatility 50', 'Strategy', NULL, NULL, 'ind_niftylowvolatility50list', false, NULL, 116);
INSERT INTO public.index_master VALUES (117, 'Nifty Alpha Low-Volatility 30', 'Strategy', NULL, NULL, 'ind_niftyalphalowvolatility30list', false, NULL, 117);
INSERT INTO public.index_master VALUES (118, 'Nifty Quality Low-Volatility 30', 'Strategy', NULL, NULL, 'ind_niftyqualitylowvolatility30list', false, NULL, 118);
INSERT INTO public.index_master VALUES (119, 'Nifty Alpha Quality Value Low-Volatility 30', 'Strategy', NULL, NULL, 'ind_niftyalphaqualityvaluelowvolatility30list', false, NULL, 119);
INSERT INTO public.index_master VALUES (120, 'Nifty Alpha Quality Low-Volatility 30', 'Strategy', NULL, NULL, 'ind_niftyalphaqualitylowvolatility30list', false, NULL, 120);
INSERT INTO public.index_master VALUES (121, 'Nifty100 Quality 30', 'Strategy', NULL, NULL, 'ind_nifty100quality30list', false, NULL, 121);
INSERT INTO public.index_master VALUES (122, 'Nifty Growth Sectors 15', 'Strategy', NULL, NULL, 'ind_niftygrowthsectors15list', false, NULL, 122);
INSERT INTO public.index_master VALUES (123, 'Nifty 50 Dividend Points', 'Strategy', NULL, NULL, 'ind_nifty50dividendpointslist', false, NULL, 123);
INSERT INTO public.index_master VALUES (124, 'Nifty50 Equal weight', 'Strategy', NULL, NULL, 'ind_nifty50equalweightlist', false, NULL, 124);
INSERT INTO public.index_master VALUES (125, 'Nifty50 PR 1x Inverse', 'Strategy', NULL, NULL, 'ind_nifty50pr1xinverselist', false, NULL, 125);
INSERT INTO public.index_master VALUES (126, 'Nifty50 PR 2x Leverage', 'Strategy', NULL, NULL, 'ind_nifty50pr2xleveragelist', false, NULL, 126);
INSERT INTO public.index_master VALUES (127, 'Nifty50 TR 1x Inverse', 'Strategy', NULL, NULL, 'ind_nifty50tr1xinverselist', false, NULL, 127);
INSERT INTO public.index_master VALUES (128, 'Nifty50 TR 2x Leverage', 'Strategy', NULL, NULL, 'ind_nifty50tr2xleveragelist', false, NULL, 128);
INSERT INTO public.index_master VALUES (129, 'Nifty50 Value 20', 'Strategy', NULL, NULL, 'ind_nifty50value20list', false, NULL, 129);
INSERT INTO public.index_master VALUES (130, 'Nifty500 Value 50', 'Strategy', NULL, NULL, 'ind_nifty500value50list', false, NULL, 130);
INSERT INTO public.index_master VALUES (131, 'Nifty50 and Short Duration Debt – Dynamic P/B', 'Strategy', NULL, NULL, 'ind_nifty50andshortdurationdebtdynamicpblist', false, NULL, 131);
INSERT INTO public.index_master VALUES (132, 'Nifty100 Alpha 30', 'Strategy', NULL, NULL, 'ind_nifty100alpha30list', false, NULL, 132);
INSERT INTO public.index_master VALUES (133, 'Nifty50 and Short Duration Debt – Dynamic P/E', 'Strategy', NULL, NULL, 'ind_nifty50andshortdurationdebtdynamicpelist', false, NULL, 133);
INSERT INTO public.index_master VALUES (134, 'Nifty200 Quality 30', 'Strategy', NULL, NULL, 'ind_nifty200quality30list', false, NULL, 134);
INSERT INTO public.index_master VALUES (135, 'Nifty Equity Savings', 'Strategy', NULL, NULL, 'ind_niftyequitysavingslist', false, NULL, 135);
INSERT INTO public.index_master VALUES (136, 'Nifty Midcap150 Quality 50', 'Strategy', NULL, NULL, 'ind_niftymidcap150quality50list', false, NULL, 136);
INSERT INTO public.index_master VALUES (137, 'Nifty200 Momentum 30', 'Strategy', NULL, NULL, 'ind_nifty200momentum30list', false, NULL, 137);
INSERT INTO public.index_master VALUES (138, 'Nifty200 Alpha 30', 'Strategy', NULL, NULL, 'ind_nifty200alpha30list', false, NULL, 138);
INSERT INTO public.index_master VALUES (139, 'Nifty Midcap150 Momentum 50', 'Strategy', NULL, NULL, 'ind_niftymidcap150momentum50list', false, NULL, 139);
INSERT INTO public.index_master VALUES (140, 'Nifty Smallcap250 Quality 50', 'Strategy', NULL, NULL, 'ind_niftysmallcap250quality50list', false, NULL, 140);
INSERT INTO public.index_master VALUES (141, 'Nifty Smallcap250 Momentum Quality 100', 'Strategy', NULL, NULL, 'ind_niftysmallcap250momentumquality100list', false, NULL, 141);
INSERT INTO public.index_master VALUES (142, 'Nifty MidSmallcap400 Momentum Quality 100', 'Strategy', NULL, NULL, 'ind_niftymidsmallcap400momentumquality100list', false, NULL, 142);
INSERT INTO public.index_master VALUES (143, 'Nifty500 Equal Weight', 'Strategy', NULL, NULL, 'ind_nifty500equalweightlist', false, NULL, 143);
INSERT INTO public.index_master VALUES (144, 'Nifty500 Momentum 50', 'Strategy', NULL, NULL, 'ind_nifty500momentum50list', false, NULL, 144);
INSERT INTO public.index_master VALUES (145, 'Nifty200 Value 30', 'Strategy', NULL, NULL, 'ind_nifty200value30list', false, NULL, 145);
INSERT INTO public.index_master VALUES (146, 'Nifty Top 10 Equal Weight', 'Strategy', NULL, NULL, 'ind_niftytop10equalweightlist', false, NULL, 146);
INSERT INTO public.index_master VALUES (147, 'Nifty500 Multicap Momentum Quality 50', 'Strategy', NULL, NULL, 'ind_nifty500multicapmomentumquality50list', false, NULL, 147);
INSERT INTO public.index_master VALUES (148, 'Nifty Top 15 Equal Weight', 'Strategy', NULL, NULL, 'ind_niftytop15equalweightlist', false, NULL, 148);
INSERT INTO public.index_master VALUES (149, 'Nifty Top 20 Equal Weight', 'Strategy', NULL, NULL, 'ind_niftytop20equalweightlist', false, NULL, 149);
INSERT INTO public.index_master VALUES (150, 'Nifty500 Quality 50', 'Strategy', NULL, NULL, 'ind_nifty500quality50list', false, NULL, 150);
INSERT INTO public.index_master VALUES (151, 'Nifty500 Low Volatility 50', 'Strategy', NULL, NULL, 'ind_nifty500lowvolatility50list', false, NULL, 151);
INSERT INTO public.index_master VALUES (152, 'Nifty500 Multifactor MQVLv 50', 'Strategy', NULL, NULL, 'ind_nifty500multifactormqvlv50list', false, NULL, 152);
INSERT INTO public.index_master VALUES (153, 'Nifty500 Flexicap Quality 30', 'Strategy', NULL, NULL, 'ind_nifty500flexicapquality30list', false, NULL, 153);
INSERT INTO public.index_master VALUES (154, 'Nifty Total Market Momentum Quality 50', 'Strategy', NULL, NULL, 'ind_niftytotalmarketmomentumquality50list', false, NULL, 154);
INSERT INTO public.index_master VALUES (155, 'Nifty500 Growth 50', 'Strategy', NULL, NULL, 'ind_nifty500growth50list', false, NULL, 155);
INSERT INTO public.index_master VALUES (156, 'Nifty High Dividend Yield 15', 'Strategy', NULL, NULL, 'ind_niftyhighdividendyield15list', false, NULL, 156);
INSERT INTO public.index_master VALUES (157, 'Nifty50 USD', 'Strategy', NULL, NULL, 'ind_nifty50usdlist', false, NULL, 157);



INSERT INTO public.sector_index_map VALUES ('Financial Services', 'Nifty Financial Services');
INSERT INTO public.sector_index_map VALUES ('Financial Services', 'Nifty MidSmall Financial Services');
INSERT INTO public.sector_index_map VALUES ('Capital Goods', 'Nifty Capital Goods');
INSERT INTO public.sector_index_map VALUES ('Capital Goods', 'Nifty India Manufacturing');
INSERT INTO public.sector_index_map VALUES ('Capital Goods', 'Nifty Infrastructure');
INSERT INTO public.sector_index_map VALUES ('Healthcare', 'Nifty Healthcare');
INSERT INTO public.sector_index_map VALUES ('Healthcare', 'Nifty Pharma');
INSERT INTO public.sector_index_map VALUES ('Healthcare', 'Nifty500 Healthcare');
INSERT INTO public.sector_index_map VALUES ('Healthcare', 'Nifty MidSmall Healthcare');
INSERT INTO public.sector_index_map VALUES ('Automobile and Auto Components', 'Nifty Auto');
INSERT INTO public.sector_index_map VALUES ('Automobile and Auto Components', 'Nifty Mobility');
INSERT INTO public.sector_index_map VALUES ('Automobile and Auto Components', 'Nifty EV & New Age Automotive');
INSERT INTO public.sector_index_map VALUES ('Fast Moving Consumer Goods', 'Nifty FMCG');
INSERT INTO public.sector_index_map VALUES ('Fast Moving Consumer Goods', 'Nifty India Consumption');
INSERT INTO public.sector_index_map VALUES ('Fast Moving Consumer Goods', 'Nifty Non-Cyclical Consumer');
INSERT INTO public.sector_index_map VALUES ('Consumer Durables', 'Nifty Consumer Durables');
INSERT INTO public.sector_index_map VALUES ('Consumer Durables', 'Nifty India Consumption');
INSERT INTO public.sector_index_map VALUES ('Consumer Services', 'Nifty Consumer Services');
INSERT INTO public.sector_index_map VALUES ('Consumer Services', 'Nifty India Consumption');
INSERT INTO public.sector_index_map VALUES ('Consumer Services', 'Nifty India New Age Consumption');
INSERT INTO public.sector_index_map VALUES ('Chemicals', 'Nifty Chemicals');
INSERT INTO public.sector_index_map VALUES ('Information Technology', 'Nifty IT');
INSERT INTO public.sector_index_map VALUES ('Information Technology', 'Nifty MidSmall IT & Telecom');
INSERT INTO public.sector_index_map VALUES ('Information Technology', 'Nifty India Digital');
INSERT INTO public.sector_index_map VALUES ('Telecommunication', 'Nifty Telecommunications');
INSERT INTO public.sector_index_map VALUES ('Telecommunication', 'Nifty MidSmall IT & Telecom');
INSERT INTO public.sector_index_map VALUES ('Metals & Mining', 'Nifty Metal');
INSERT INTO public.sector_index_map VALUES ('Metals & Mining', 'Nifty Commodities');
INSERT INTO public.sector_index_map VALUES ('Oil Gas & Consumable Fuels', 'Nifty Oil & Gas');
INSERT INTO public.sector_index_map VALUES ('Oil Gas & Consumable Fuels', 'Nifty Energy');
INSERT INTO public.sector_index_map VALUES ('Oil Gas & Consumable Fuels', 'Nifty Commodities');
INSERT INTO public.sector_index_map VALUES ('Power', 'Nifty Power');
INSERT INTO public.sector_index_map VALUES ('Power', 'Nifty Energy');
INSERT INTO public.sector_index_map VALUES ('Utilities', 'Nifty Energy');
INSERT INTO public.sector_index_map VALUES ('Utilities', 'Nifty Infrastructure');
INSERT INTO public.sector_index_map VALUES ('Construction', 'Nifty Construction');
INSERT INTO public.sector_index_map VALUES ('Construction', 'Nifty Infrastructure');
INSERT INTO public.sector_index_map VALUES ('Construction', 'Nifty Core Housing');
INSERT INTO public.sector_index_map VALUES ('Construction Materials', 'Nifty Cement');
INSERT INTO public.sector_index_map VALUES ('Construction Materials', 'Nifty Commodities');
INSERT INTO public.sector_index_map VALUES ('Construction Materials', 'Nifty Core Housing');
INSERT INTO public.sector_index_map VALUES ('Realty', 'Nifty Realty');
INSERT INTO public.sector_index_map VALUES ('Realty', 'Nifty REITs & Realty');
INSERT INTO public.sector_index_map VALUES ('Realty', 'Nifty Housing');
INSERT INTO public.sector_index_map VALUES ('Services', 'Nifty Services Sector');
INSERT INTO public.sector_index_map VALUES ('Services', 'Nifty Commercial & Transport Services');
INSERT INTO public.sector_index_map VALUES ('Media Entertainment & Publication', 'Nifty Media');



SELECT pg_catalog.setval('public.index_master_id_seq', 157, true);

-- TimescaleDB objects, as the Alembic revisions created them.
SELECT create_hypertable('public.ohlcv_daily', 'date', chunk_time_interval => INTERVAL '1 year', create_default_indexes => FALSE);
SELECT create_hypertable('public.factor_daily', 'date', chunk_time_interval => INTERVAL '1 year', create_default_indexes => FALSE);
SELECT create_hypertable('public.index_member_daily', 'date', chunk_time_interval => INTERVAL '1 year', create_default_indexes => FALSE);
SELECT create_hypertable('public.index_snapshot_daily', 'date', chunk_time_interval => INTERVAL '1 year', create_default_indexes => FALSE);
SELECT create_hypertable('public.market_health_daily', 'date', chunk_time_interval => INTERVAL '1 year', create_default_indexes => FALSE);
SELECT create_hypertable('public.eq_minute_bar', 'ts', chunk_time_interval => INTERVAL '1 month', create_default_indexes => FALSE);

ALTER TABLE public.ohlcv_daily SET (timescaledb.compress, timescaledb.compress_segmentby = 'instrument_id');
SELECT add_compression_policy('public.ohlcv_daily', INTERVAL '90 days');

CREATE MATERIALIZED VIEW public.market_health_monthly
WITH (timescaledb.continuous) AS
SELECT
    index_id,
    time_bucket(INTERVAL '1 month', date) AS bucket,
    avg(pct_above_200dma)     AS pct_above_200dma,
    avg(pct_above_50dma)      AS pct_above_50dma,
    avg(pct_within_10pct_ath) AS pct_within_10pct_ath,
    avg(pct_ret_1y_positive)  AS pct_ret_1y_positive,
    max(constituent_count)    AS constituent_count,
    count(*)                  AS trading_days
FROM public.market_health_daily
GROUP BY index_id, bucket
WITH NO DATA;

CREATE MATERIALIZED VIEW public.index_snapshot_monthly
WITH (timescaledb.continuous) AS
SELECT
    index_id,
    time_bucket(INTERVAL '1 month', date) AS bucket,
    first(level, date) AS open_level,
    last(level, date)  AS close_level,
    min(level)         AS low_level,
    max(level)         AS high_level,
    count(*)           AS trading_days
FROM public.index_snapshot_daily
GROUP BY index_id, bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy('public.market_health_monthly', start_offset => INTERVAL '3 months', end_offset => INTERVAL '1 day', schedule_interval => INTERVAL '1 day');
SELECT add_continuous_aggregate_policy('public.index_snapshot_monthly', start_offset => INTERVAL '3 months', end_offset => INTERVAL '1 day', schedule_interval => INTERVAL '1 day');

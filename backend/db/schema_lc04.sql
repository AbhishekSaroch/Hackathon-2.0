-- PortfolioIQ LC-04 (self-building operations command dashboard) schema. Lives in its own file: lc04.db.
-- Rules: every table has tenant_id, and all access goes through db.py with database="lc04".
--   tenant_id = the LENDER for data tables (5 lenders). Shared governance tables (metric definitions, roles, users,
--   dimension list, request set, replay pointer) use the fixed tenant_id 'GLOBAL' so the same
--   ":tenant_id" rule applies to every query.
-- Time: all timestamps are ISO text 'YYYY-MM-DD HH:MM:SS' in one simulated timezone. bucket_ts = start of the 15-minute bucket.
-- Days 1-30 of the stream are PUBLIC (we tune on them), days 31-40 are HIDDEN (run once, ledgered).
-- Sections: INPUT (what the brief gives us, we generate it), DERIVED (cube), GOVERNANCE, EVALUATION KEY, AGENT OUTPUT.

PRAGMA foreign_keys = ON;

-- ============================================================ INPUT

CREATE TABLE IF NOT EXISTS lc_d13 (                  -- D13 (our own definition): the dimension values used for drill-down
    tenant_id      TEXT NOT NULL,                    -- 'GLOBAL'
    dimension      TEXT NOT NULL,                    -- city | agency | dialer | gateway | product | bucket
    value_id       TEXT NOT NULL,
    label          TEXT,
    parent_id      TEXT,                             -- e.g. a dialer belongs to a city site
    PRIMARY KEY (tenant_id, dimension, value_id)
);

CREATE TABLE IF NOT EXISTS lc_calendar (             -- known seasonality: the detector MAY use this (weekends, salary days, month end, festivals)
    tenant_id      TEXT NOT NULL,                    -- 'GLOBAL'
    day            TEXT NOT NULL,
    kind           TEXT NOT NULL,                    -- weekend | salary_day | month_end | festival
    city           TEXT NOT NULL DEFAULT '*',        -- '*' = national, otherwise a city (regional festival)
    note           TEXT,
    PRIMARY KEY (tenant_id, day, kind, city)
);

CREATE TABLE IF NOT EXISTS lc_accounts (             -- D1 columns + D13 attributes. No customer names (not needed here).
    account_id     TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,                    -- the lender
    product        TEXT,
    loan_amount    REAL,
    emi_amount     REAL,
    dpd            INTEGER DEFAULT 0,
    bucket         TEXT,                             -- DPD bucket: 0 | 1-30 | 31-60 | 61-90 | 90+
    outstanding    REAL,
    city           TEXT,                             -- D13
    agency_id      TEXT                              -- D13: the agency that works this account
);

CREATE TABLE IF NOT EXISTS lc_events (               -- intraday event stream (contacts, PTPs, payments, dispositions)
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    account_id     TEXT NOT NULL REFERENCES lc_accounts(account_id),
    ts             TEXT NOT NULL,                    -- when it happened
    ingested_at    TEXT NOT NULL,                    -- when the system received it (>= ts). Replay only shows rows with ingested_at <= clock
    event_type     TEXT NOT NULL CHECK (event_type IN ('contact', 'disposition', 'ptp', 'payment', 'legal')),
    outcome        TEXT,                             -- contact: connected | no_answer | failed
                                                     -- disposition: rpc | ptp | refused | dispute | wrong_number | callback
                                                     -- ptp: created | kept | broken
                                                     -- payment: success | failed
                                                     -- legal: sent (a legal communication; ts = when it was SENT, judged against the 08:00-20:00 window)
    channel        TEXT,                             -- call | sms | whatsapp (contact events)
    agency_id      TEXT,                             -- contact / disposition / ptp events
    dialer_id      TEXT,                             -- contact / disposition events
    gateway_id     TEXT,                             -- payment events
    amount         REAL                              -- ptp amount or payment amount
);

-- ============================================================ DERIVED

CREATE TABLE IF NOT EXISTS cube_15m (                -- events pre-aggregated per 15 min per segment. Dashboards and detector read ONLY this.
    tenant_id      TEXT NOT NULL,
    bucket_ts      TEXT NOT NULL,                    -- start of the 15-minute bucket
    day            TEXT NOT NULL,
    slot           INTEGER NOT NULL,                 -- 0..95, slot of the day (seasonality key)
    dow            INTEGER NOT NULL,                 -- 0 = Monday .. 6 = Sunday (seasonality key)
    city           TEXT NOT NULL,
    agency_id      TEXT NOT NULL,                    -- '-' when the event has none
    dialer_id      TEXT NOT NULL,                    -- '-' when the event has none
    gateway_id     TEXT NOT NULL,                    -- '-' when the event has none
    product        TEXT NOT NULL,
    bucket         TEXT NOT NULL,
    -- base counters (every governed metric is a formula over these)
    ev_total       INTEGER NOT NULL DEFAULT 0,       -- all events (volume / freshness / duplicates)
    contacts_attempted INTEGER NOT NULL DEFAULT 0,
    contacts_connected INTEGER NOT NULL DEFAULT 0,
    disp_total     INTEGER NOT NULL DEFAULT 0,
    disp_rpc       INTEGER NOT NULL DEFAULT 0,
    disp_ptp       INTEGER NOT NULL DEFAULT 0,
    disp_refused   INTEGER NOT NULL DEFAULT 0,
    disp_dispute   INTEGER NOT NULL DEFAULT 0,
    disp_other     INTEGER NOT NULL DEFAULT 0,
    ptp_created    INTEGER NOT NULL DEFAULT 0,
    ptp_amount     REAL    NOT NULL DEFAULT 0,
    ptp_kept       INTEGER NOT NULL DEFAULT 0,
    ptp_broken     INTEGER NOT NULL DEFAULT 0,
    pay_attempts   INTEGER NOT NULL DEFAULT 0,
    pay_success    INTEGER NOT NULL DEFAULT 0,
    pay_failed     INTEGER NOT NULL DEFAULT 0,
    pay_amount     REAL    NOT NULL DEFAULT 0,       -- amount of successful payments
    legal_sent     INTEGER NOT NULL DEFAULT 0,       -- legal communications (event_type 'legal')
    legal_out_of_window INTEGER NOT NULL DEFAULT 0,  -- ... sent outside the permitted 08:00-20:00 window (judged on the SEND time ts)
    PRIMARY KEY (tenant_id, bucket_ts, city, agency_id, dialer_id, gateway_id, product, bucket)
) WITHOUT ROWID;

-- ============================================================ GOVERNANCE (shared, tenant_id = 'GLOBAL')

CREATE TABLE IF NOT EXISTS metric_defs (             -- the ONLY allowed definitions (~60). Mirror of the code registry in backend/metrics/
    metric_id      TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,                    -- 'GLOBAL'
    name           TEXT NOT NULL,
    description    TEXT,
    unit           TEXT,                             -- count | pct | rs | ratio
    numerator      TEXT NOT NULL,                    -- expression over cube counters, e.g. 'pay_success'
    denominator    TEXT,                             -- NULL for plain sums
    good_direction TEXT CHECK (good_direction IN ('up', 'down', 'neutral')),
    allowed_dims   TEXT,                             -- JSON list of dimensions it may be sliced by
    is_key         INTEGER NOT NULL DEFAULT 0,       -- 1 = monitored by the detector
    owner          TEXT,
    version        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, metric_id)
);

CREATE TABLE IF NOT EXISTS lc_roles (                -- 5 roles
    role_id        TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,                    -- 'GLOBAL'
    name           TEXT NOT NULL,
    scope_type     TEXT NOT NULL CHECK (scope_type IN ('all_lenders', 'lender_set', 'single_lender')),
    allowed_metrics TEXT,                            -- JSON list of metric ids, or '*'
    allowed_dims   TEXT,                             -- JSON list of dimensions
    default_request TEXT,                            -- what the role sees when it asks nothing
    PRIMARY KEY (tenant_id, role_id)
);

CREATE TABLE IF NOT EXISTS lc_users (                -- demo users (the role switcher). Scope comes from here, never from a prompt.
    user_id        TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,                    -- 'GLOBAL'
    display_name   TEXT,
    role_id        TEXT NOT NULL,
    lender_scope   TEXT,                             -- JSON list of lender ids (single_lender = 1 id, all_lenders = '*')
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS dashboard_requests (      -- plain-language requests by role: 10 public, 10 hidden
    request_id     TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,                    -- 'GLOBAL'
    role_id        TEXT NOT NULL,
    request_text   TEXT NOT NULL,
    split          TEXT NOT NULL CHECK (split IN ('public', 'hidden')),
    PRIMARY KEY (tenant_id, request_id)
);

CREATE TABLE IF NOT EXISTS lc_meta (                 -- stream settings + the replay clock
    tenant_id      TEXT NOT NULL,                    -- 'GLOBAL'
    key            TEXT NOT NULL,                    -- stream_start | public_days | hidden_days | clock | threshold_version
    value          TEXT,
    PRIMARY KEY (tenant_id, key)
);

-- ============================================================ EVALUATION KEY (the agent NEVER reads these)

CREATE TABLE IF NOT EXISTS anomaly_key (             -- PUBLIC period: 3 practice anomalies we seed to tune on
    anomaly_id     TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,                    -- the lender it belongs to, or 'GLOBAL' when it spans lenders
    kind           TEXT NOT NULL,                    -- dialer_outage | agency_misdisposition | gateway_failure | feed_stall | ptp_collapse | duplicate_events
    start_ts       TEXT NOT NULL,
    end_ts         TEXT NOT NULL,
    metric_id      TEXT,                             -- the metric that shows it most
    segment        TEXT NOT NULL,                    -- JSON, e.g. {"city": "Pune", "dialer": "D2"}
    note           TEXT,
    PRIMARY KEY (tenant_id, anomaly_id)
);

CREATE TABLE IF NOT EXISTS anomaly_key_hidden (      -- HIDDEN period: the 6 real anomalies. ONLY the evaluator may read this.
    anomaly_id     TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,
    kind           TEXT NOT NULL,
    start_ts       TEXT NOT NULL,
    end_ts         TEXT NOT NULL,
    metric_id      TEXT,
    segment        TEXT NOT NULL,
    note           TEXT,
    PRIMARY KEY (tenant_id, anomaly_id)
);

-- ============================================================ AGENT OUTPUT

CREATE TABLE IF NOT EXISTS agent_runs (              -- one row per detector tick / drill-down / spec generation
    run_id         TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    agent_name     TEXT NOT NULL,                    -- lc04_detect | lc04_drill | lc04_alert | lc04_spec
    entity_id      TEXT,                             -- bucket_ts, anomaly_id or dashboard_id
    status         TEXT DEFAULT 'running',
    started_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at    TEXT
);

CREATE TABLE IF NOT EXISTS agent_steps (             -- tool calls shown in the UI timeline
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    run_id         TEXT NOT NULL REFERENCES agent_runs(run_id),
    step_no        INTEGER NOT NULL,
    tool           TEXT,
    input          TEXT,
    output         TEXT,
    ts             TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS anomalies (               -- what the detector found, after removing seasonality
    anomaly_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,                    -- the lender (an incident that spans lenders gets one row per lender)
    incident_id    TEXT,                             -- groups detections that share one root cause (one alert per incident)
    metric_id      TEXT NOT NULL,
    onset_ts       TEXT NOT NULL,                    -- first bucket that deviated
    detected_at    TEXT NOT NULL,                    -- replay clock when it fired (detected_at - onset_ts = time to detect)
    observed       REAL,
    expected       REAL,                             -- seasonal baseline for the same slot and weekday
    score          REAL,                             -- robust deviation (MAD units)
    buckets_persisted INTEGER,
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'suppressed')),
    threshold_version INTEGER
);

CREATE TABLE IF NOT EXISTS annotations (             -- the drill-down finding, pinned on the dashboard
    annotation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    anomaly_id     INTEGER NOT NULL REFERENCES anomalies(anomaly_id),
    created_at     TEXT NOT NULL,                    -- replay clock
    segment        TEXT NOT NULL,                    -- JSON, e.g. {"city": "Pune", "dialer": "D2"}
    share_of_deviation REAL,                         -- how much of the gap this segment explains (0..1)
    counterfactual_ok INTEGER,                       -- 1 = removing the segment brings the metric back to baseline
    evidence       TEXT,                             -- JSON: the computed numbers behind the finding
    headline       TEXT                              -- templated from the numbers, never written by the LLM
);

CREATE TABLE IF NOT EXISTS alerts (                  -- mock alerting: nothing is sent outside
    alert_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    incident_id    TEXT,
    anomaly_id     INTEGER REFERENCES anomalies(anomaly_id),
    annotation_id  INTEGER REFERENCES annotations(annotation_id),
    created_at     TEXT NOT NULL,                    -- replay clock
    audience_role  TEXT,
    line1          TEXT NOT NULL,                    -- what happened
    line2          TEXT NOT NULL,                    -- where it comes from (the driving segment)
    link           TEXT NOT NULL,                    -- annotated view
    status         TEXT NOT NULL DEFAULT 'sent_mock' CHECK (status IN ('sent_mock', 'suppressed'))
);

CREATE TABLE IF NOT EXISTS dashboards (              -- generated view specs
    dashboard_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,                    -- the viewer's lender, or 'GLOBAL' for CN-wide roles
    user_id        TEXT,
    role_id        TEXT NOT NULL,
    request_text   TEXT,
    spec           TEXT NOT NULL,                    -- JSON: metrics, charts, filters (governed metric ids only)
    source         TEXT CHECK (source IN ('llm', 'fallback', 'rebuilt')),
    focus_anomaly_id INTEGER REFERENCES anomalies(anomaly_id),   -- set when rebuilt around a finding
    generation_ms  INTEGER,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================ INDEXES
CREATE INDEX IF NOT EXISTS ix_lc_events_acct  ON lc_events(tenant_id, account_id);
CREATE INDEX IF NOT EXISTS ix_lc_events_ts    ON lc_events(tenant_id, ts);
CREATE INDEX IF NOT EXISTS ix_lc_events_ing   ON lc_events(tenant_id, ingested_at);
CREATE INDEX IF NOT EXISTS ix_lc_acct_dims    ON lc_accounts(tenant_id, city, agency_id);
CREATE INDEX IF NOT EXISTS ix_cube_day        ON cube_15m(tenant_id, day, slot);
CREATE INDEX IF NOT EXISTS ix_cube_city       ON cube_15m(tenant_id, city, bucket_ts);
CREATE INDEX IF NOT EXISTS ix_cube_agency     ON cube_15m(tenant_id, agency_id, bucket_ts);
CREATE INDEX IF NOT EXISTS ix_anom_time       ON anomalies(tenant_id, detected_at);
CREATE INDEX IF NOT EXISTS ix_anom_incident   ON anomalies(incident_id);
CREATE INDEX IF NOT EXISTS ix_alerts_incident ON alerts(tenant_id, incident_id);
CREATE INDEX IF NOT EXISTS ix_steps_run       ON agent_steps(run_id, step_no);

-- Indexes on the foreign-key columns (BI-03 lesson: without them a delete scans every child table).
CREATE INDEX IF NOT EXISTS ix_lc_events_fk    ON lc_events(account_id);
CREATE INDEX IF NOT EXISTS ix_annot_fk        ON annotations(anomaly_id);
CREATE INDEX IF NOT EXISTS ix_alerts_fk_anom  ON alerts(anomaly_id);
CREATE INDEX IF NOT EXISTS ix_alerts_fk_annot ON alerts(annotation_id);
CREATE INDEX IF NOT EXISTS ix_dash_fk         ON dashboards(focus_anomaly_id);
CREATE INDEX IF NOT EXISTS ix_steps_fk        ON agent_steps(run_id);

-- PortfolioIQ BI-03 (pre-delinquency early warning) schema. Lives in its own file: bi03.db.
-- Rules: every table has tenant_id, and all access goes through db.py with database="bi03".
-- INPUT tables follow the BI-03 brief only (no extra customer data).
-- OUTPUT tables are what the agent writes.
-- POINT-IN-TIME rule: a feature on day D may only use rows with available_from <= D.

PRAGMA foreign_keys = ON;

-- ============================================================ INPUT

CREATE TABLE IF NOT EXISTS bp_accounts (            -- "current-account book": SAME columns as D1 (accounts)
    account_id     TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    customer_name  TEXT,
    product        TEXT,
    branch         TEXT,
    loan_amount    REAL,
    emi_amount     REAL,
    due_date       TEXT,
    dpd            INTEGER DEFAULT 0,
    bucket         TEXT,
    outstanding    REAL,
    status         TEXT DEFAULT 'current'            -- every account is current at the start
);

CREATE TABLE IF NOT EXISTS events (                 -- daily event stream (D8 history = rows before the stream start)
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    account_id     TEXT NOT NULL REFERENCES bp_accounts(account_id),
    event_date     TEXT NOT NULL,                    -- day it happened (ISO date)
    available_from TEXT NOT NULL,                    -- first day the system may use it (>= event_date)
    source         TEXT NOT NULL CHECK (source IN ('statement', 'bureau', 'repayment')),
    event_type     TEXT NOT NULL,                    -- statement: credit | debit
                                                     -- bureau: new_enquiry | other_loan_bounce
                                                     -- repayment: emi_due | emi_paid
    amount         REAL,                             -- money amount
    value          REAL,                             -- non-money number (e.g. days late, utilisation)
    balance        REAL,                             -- statement events only
    narration      TEXT                              -- statement narration (e.g. SALARY ...)
);

CREATE TABLE IF NOT EXISTS bureau_snapshots (       -- D9: monthly bureau refresh
    snapshot_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id             TEXT NOT NULL,
    account_id            TEXT NOT NULL REFERENCES bp_accounts(account_id),
    snapshot_date         TEXT NOT NULL,             -- as-of date of the refresh
    available_from        TEXT NOT NULL,             -- bureau data arrives with a lag
    card_utilisation      REAL,                      -- 0..1
    enquiries_30d         INTEGER,
    open_loans            INTEGER,
    other_loan_dpd        INTEGER,                   -- days past due on the borrower's OTHER loans
    other_loan_bounces_90d INTEGER,
    bureau_score          INTEGER
);

CREATE TABLE IF NOT EXISTS outcome_labels (         -- PUBLIC period only: who went DPD 1, and when
    account_id     TEXT PRIMARY KEY REFERENCES bp_accounts(account_id),
    tenant_id      TEXT NOT NULL,
    dpd1_date      TEXT                              -- NULL = stayed current in the public period
);

CREATE TABLE IF NOT EXISTS outcome_labels_hidden (  -- hidden period. ONLY the evaluator may read this.
    account_id     TEXT PRIMARY KEY REFERENCES bp_accounts(account_id),   -- Training / feature code must never touch it.
    tenant_id      TEXT NOT NULL,
    dpd1_date      TEXT
);

CREATE TABLE IF NOT EXISTS service_templates (      -- D11: approved service message templates (nothing else may be sent)
    template_id    TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,                    -- each lender has its own template set, ids may repeat across lenders
    nudge_type     TEXT NOT NULL CHECK (nudge_type IN ('emi_date_change', 'autopay_setup', 'reminder')),
    language       TEXT NOT NULL DEFAULT 'hinglish', -- hinglish (Roman letters) | english
    channel        TEXT NOT NULL DEFAULT 'whatsapp',
    body           TEXT NOT NULL,                    -- with placeholders like {name}, {new_date}
    placeholders   TEXT,                             -- comma separated names used in body
    PRIMARY KEY (tenant_id, template_id)
);

-- ============================================================ OUTPUT

CREATE TABLE IF NOT EXISTS risk_scores (            -- distress state per account per day
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id        TEXT NOT NULL,
    account_id       TEXT NOT NULL REFERENCES bp_accounts(account_id),
    as_of_date       TEXT NOT NULL,
    score            REAL NOT NULL,                  -- risk of missing an EMI soon, 0..1
    dominant_signal  TEXT,                           -- e.g. salary_delay | other_loan_bounce | card_utilisation | enquiries
    signals_json     TEXT,                           -- contribution of each signal
    model_version    TEXT NOT NULL,
    UNIQUE (tenant_id, account_id, as_of_date, model_version)
);

CREATE TABLE IF NOT EXISTS flags (                  -- accounts that crossed the threshold
    flag_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id          TEXT NOT NULL,
    account_id         TEXT NOT NULL REFERENCES bp_accounts(account_id),
    flagged_on         TEXT NOT NULL,
    score              REAL NOT NULL,
    dominant_signal    TEXT,
    signals_json       TEXT,
    threshold_version  TEXT,
    confirmed_by_refresh INTEGER DEFAULT 0,          -- 1 = a fresh bureau / AA pull re-confirmed a borderline flag
    status             TEXT DEFAULT 'open'           -- open | nudged | closed
);

CREATE TABLE IF NOT EXISTS nudges (                 -- the service message for a flag, after the compliance gate
    nudge_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    flag_id        INTEGER NOT NULL REFERENCES flags(flag_id),
    account_id     TEXT NOT NULL REFERENCES bp_accounts(account_id),
    nudge_type     TEXT NOT NULL,                    -- emi_date_change | autopay_setup | reminder
    template_id    TEXT,
    message        TEXT,
    gate_passed    INTEGER,                          -- 1 / 0
    gate_result    TEXT,                             -- JSON: each check and why it passed or failed
    status         TEXT DEFAULT 'drafted',           -- drafted | blocked | pending_approval | sent
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    sent_at        TEXT,
    dispatch_ref   TEXT,                             -- reference from the (mock) dispatch tool
    FOREIGN KEY (tenant_id, template_id) REFERENCES service_templates(tenant_id, template_id)
);

CREATE TABLE IF NOT EXISTS flag_outcomes (          -- what really happened, so the threshold can be tuned
    flag_id        INTEGER PRIMARY KEY REFERENCES flags(flag_id),
    tenant_id      TEXT NOT NULL,
    account_id     TEXT NOT NULL REFERENCES bp_accounts(account_id),
    went_dpd1      INTEGER,                          -- 1 / 0
    dpd1_date      TEXT,
    lead_time_days INTEGER,                          -- dpd1_date minus flagged_on
    evaluated_on   TEXT
);

CREATE TABLE IF NOT EXISTS thresholds (             -- versioned threshold + how it performed
    threshold_version TEXT NOT NULL,
    tenant_id         TEXT NOT NULL,
    value             REAL NOT NULL,                 -- score cut-off
    top_pct           REAL,                          -- share of accounts flagged (e.g. 0.05)
    precision_top     REAL,
    lift              REAL,                          -- precision / base rate
    median_lead_days  REAL,
    is_active         INTEGER DEFAULT 0,
    notes             TEXT,
    created_at        TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, threshold_version)
);

CREATE TABLE IF NOT EXISTS agent_runs (             -- one row per daily run / per agent action
    run_id         TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    agent_name     TEXT NOT NULL,                    -- bi03_daily | bi03_confirm | bi03_nudge
    entity_id      TEXT,                             -- date or account_id
    status         TEXT DEFAULT 'running',
    started_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at    TEXT
);

CREATE TABLE IF NOT EXISTS agent_steps (            -- tool calls shown in the UI timeline
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    run_id         TEXT NOT NULL REFERENCES agent_runs(run_id),
    step_no        INTEGER NOT NULL,
    tool           TEXT,
    input          TEXT,
    output         TEXT,
    ts             TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bp_meta (                -- stream settings + the replay pointer
    tenant_id      TEXT NOT NULL,
    key            TEXT NOT NULL,                    -- stream_start_date | public_days | hidden_days | replay_date | model_version
    value          TEXT,
    PRIMARY KEY (tenant_id, key)
);

-- ============================================================ INDEXES
CREATE INDEX IF NOT EXISTS ix_events_acct   ON events(tenant_id, account_id, available_from);
CREATE INDEX IF NOT EXISTS ix_events_day    ON events(tenant_id, available_from);
CREATE INDEX IF NOT EXISTS ix_events_type   ON events(tenant_id, source, event_type);
CREATE INDEX IF NOT EXISTS ix_bureau_acct   ON bureau_snapshots(tenant_id, account_id, available_from);
CREATE INDEX IF NOT EXISTS ix_scores_day    ON risk_scores(tenant_id, as_of_date, model_version);
CREATE INDEX IF NOT EXISTS ix_flags_acct    ON flags(tenant_id, account_id, flagged_on);
CREATE INDEX IF NOT EXISTS ix_nudges_flag   ON nudges(tenant_id, flag_id);
CREATE INDEX IF NOT EXISTS ix_steps_run     ON agent_steps(run_id, step_no);

-- Indexes on the foreign-key columns. Without them, deleting ONE account makes SQLite scan every child table
-- (events, risk_scores ...) once per account, which is minutes at 6,000 accounts and hopeless at 60,000.
CREATE INDEX IF NOT EXISTS ix_events_fk     ON events(account_id);
CREATE INDEX IF NOT EXISTS ix_bureau_fk     ON bureau_snapshots(account_id);
CREATE INDEX IF NOT EXISTS ix_scores_fk     ON risk_scores(account_id);
CREATE INDEX IF NOT EXISTS ix_flags_fk      ON flags(account_id);
CREATE INDEX IF NOT EXISTS ix_nudges_fk     ON nudges(account_id);
CREATE INDEX IF NOT EXISTS ix_nudges_flagid ON nudges(flag_id);
CREATE INDEX IF NOT EXISTS ix_nudges_tpl    ON nudges(tenant_id, template_id);
CREATE INDEX IF NOT EXISTS ix_outcomes_fk   ON flag_outcomes(account_id);

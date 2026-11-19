-- PortfolioIQ schema (SQLite). Step 1 of BI-02.
-- Rule: every table carries tenant_id. All access goes through db.py (adds tenant filter).
-- Input tables: accounts, repayments, calls, promises, statement_txns, gold_labels
-- Output tables: assessments, evidence, agent_runs, agent_steps, recommendations
-- Shared: metrics (for LC-01 / LC-04 later)

PRAGMA foreign_keys = ON;

-- ---------- INPUT TABLES ----------

CREATE TABLE IF NOT EXISTS accounts (            -- D1
    account_id     TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    customer_name  TEXT,
    product        TEXT,
    branch         TEXT,
    loan_amount    REAL,
    emi_amount     REAL,
    due_date       TEXT,                          -- ISO date
    dpd            INTEGER DEFAULT 0,
    bucket         TEXT,
    outstanding    REAL,
    status         TEXT DEFAULT 'delinquent'      -- 'current' | 'delinquent' (BI-03 uses 'current')
);

CREATE TABLE IF NOT EXISTS repayments (          -- D3
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    account_id     TEXT NOT NULL REFERENCES accounts(account_id),
    due_date       TEXT NOT NULL,
    paid_date      TEXT,                          -- NULL = not paid
    amount_paid    REAL,
    status         TEXT                           -- 'paid' | 'late' | 'missed'
);

CREATE TABLE IF NOT EXISTS calls (               -- D5
    call_id        TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    account_id     TEXT NOT NULL REFERENCES accounts(account_id),
    call_date      TEXT NOT NULL,
    agent_id       TEXT,
    transcript     TEXT,                          -- code-mixed, ASR noise
    stated_reason_raw TEXT
);

CREATE TABLE IF NOT EXISTS promises (            -- D6
    ptp_id         TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    account_id     TEXT NOT NULL REFERENCES accounts(account_id),
    promised_date  TEXT,
    promised_amount REAL,
    kept           INTEGER                        -- 1 / 0
);

CREATE TABLE IF NOT EXISTS statement_txns (      -- D8
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    account_id     TEXT NOT NULL REFERENCES accounts(account_id),
    pull_no        INTEGER NOT NULL DEFAULT 1,    -- 1 = original, 2 = fresh pull (mock AA)
    txn_date       TEXT NOT NULL,                 -- point-in-time safe (BI-03)
    narration      TEXT,
    amount         REAL,
    type           TEXT,                          -- 'credit' | 'debit'
    balance        REAL
);

CREATE TABLE IF NOT EXISTS gold_labels (         -- expert labels (300 public, 500 hidden later)
    account_id     TEXT PRIMARY KEY REFERENCES accounts(account_id),
    tenant_id      TEXT NOT NULL,
    expert_label   TEXT NOT NULL,                 -- forgot|cant_pay|wont_pay|disputed|insufficient
    rationale      TEXT,
    is_ambiguous   INTEGER DEFAULT 0,
    split          TEXT DEFAULT 'public'          -- 'public' | 'hidden'
);

-- ---------- OUTPUT TABLES ----------

CREATE TABLE IF NOT EXISTS agent_runs (          -- generic: any agent (bi02, bi03, ask...)
    run_id         TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    agent_name     TEXT NOT NULL,
    entity_id      TEXT,                          -- account_id etc.
    status         TEXT DEFAULT 'running',        -- running | done | failed
    started_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at    TEXT
);

CREATE TABLE IF NOT EXISTS agent_steps (         -- trace shown in UI timeline
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    run_id         TEXT NOT NULL REFERENCES agent_runs(run_id),
    step_no        INTEGER NOT NULL,
    tool           TEXT,
    input          TEXT,                          -- JSON
    output         TEXT,                          -- JSON
    ts             TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS assessments (         -- BI-02 result (written back to account record)
    assessment_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    account_id     TEXT NOT NULL REFERENCES accounts(account_id),
    run_id         TEXT REFERENCES agent_runs(run_id),
    label          TEXT NOT NULL,                 -- forgot|cant_pay|wont_pay|disputed|insufficient
    confidence     REAL,                          -- calibrated, 0..1
    reasoning_summary TEXT,
    as_of_date     TEXT,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS evidence (            -- every label cites evidence
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    assessment_id  INTEGER NOT NULL REFERENCES assessments(assessment_id),
    source         TEXT,                          -- D3 | D5 | D6 | D8
    source_ref     TEXT,                          -- call_id / ptp_id / txn id
    text           TEXT,
    supports       TEXT                           -- which label it supports / contradicts
);

CREATE TABLE IF NOT EXISTS recommendations (     -- Next Action layer (BI-02 and BI-03 feed this)
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    account_id     TEXT NOT NULL REFERENCES accounts(account_id),
    source         TEXT,                          -- 'bi02' | 'bi03'
    source_ref     INTEGER,                       -- assessment_id etc.
    channel        TEXT,                          -- whatsapp | telecall | field_visit | dispute_team | human
    action         TEXT,
    reason         TEXT,
    status         TEXT DEFAULT 'pending',        -- pending | approved | skipped | sent
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ---------- SHARED ----------

CREATE TABLE IF NOT EXISTS metrics (             -- governed definitions for LC-01 / LC-04 (empty for now)
    name           TEXT PRIMARY KEY,
    definition     TEXT,
    sql            TEXT
);

-- ---------- INDEXES ----------
CREATE INDEX IF NOT EXISTS ix_repay_acct  ON repayments(tenant_id, account_id);
CREATE INDEX IF NOT EXISTS ix_calls_acct  ON calls(tenant_id, account_id);
CREATE INDEX IF NOT EXISTS ix_ptp_acct    ON promises(tenant_id, account_id);
CREATE INDEX IF NOT EXISTS ix_stmt_acct   ON statement_txns(tenant_id, account_id, pull_no);
CREATE INDEX IF NOT EXISTS ix_assess_acct ON assessments(tenant_id, account_id);
CREATE INDEX IF NOT EXISTS ix_steps_run   ON agent_steps(run_id, step_no);

-- LC-01 (Ask-your-portfolio agent) warehouse. Own database file: lc01.db. Nothing here touches BI-02 / BI-03 / LC-04.
-- Every table carries tenant_id (= lender id L1..L3). The query layer (db.py) refuses any query without :tenant_id.
-- Trimmed warehouse (11 tables instead of the brief's ~40): the 5 fact tables are denormalised on purpose (city, region,
-- product, agency are copied onto every row) so one governed metric is one SELECT over one table.

-- ---------- dimensions ----------
CREATE TABLE IF NOT EXISTS lender_config (          -- lender-specific quirks (shown as a note next to the answer)
    tenant_id     TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    grace_days    INTEGER NOT NULL,                 -- days after the due date before DPD starts counting
    npa_dpd       INTEGER NOT NULL,
    quirk_note    TEXT NOT NULL,
    PRIMARY KEY (tenant_id)
);

CREATE TABLE IF NOT EXISTS agencies (
    tenant_id   TEXT NOT NULL,
    agency      TEXT NOT NULL,
    agency_type TEXT NOT NULL,                      -- tele | field | hybrid
    PRIMARY KEY (tenant_id, agency)
);

CREATE TABLE IF NOT EXISTS loans (                  -- D1-style account table (no customer name, no protected attributes)
    tenant_id        TEXT NOT NULL,
    loan_id          TEXT NOT NULL,
    product          TEXT NOT NULL,
    city             TEXT NOT NULL,
    region           TEXT NOT NULL,
    agency           TEXT NOT NULL,
    vintage          TEXT NOT NULL,                 -- disbursal quarter, e.g. 2025Q3
    disbursed_amount REAL NOT NULL,
    emi              REAL NOT NULL,
    tenure_months    INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, loan_id)
);

-- ---------- facts ----------
CREATE TABLE IF NOT EXISTS fact_perf (              -- one row per loan per month (repayment performance, D3/D6 style)
    tenant_id       TEXT NOT NULL,
    month           TEXT NOT NULL,                  -- 'YYYY-MM'
    loan_id         TEXT NOT NULL,
    product         TEXT NOT NULL,
    city            TEXT NOT NULL,
    region          TEXT NOT NULL,
    agency          TEXT NOT NULL,
    vintage         TEXT NOT NULL,
    bucket          TEXT NOT NULL,                  -- OPENING bucket: current | 1-30 | 31-60 | 61-90 | 90+
    dpd_start       INTEGER NOT NULL,
    dpd_end         INTEGER NOT NULL,
    emi_due         REAL NOT NULL,
    arrears_start   REAL NOT NULL,
    paid_emi        REAL NOT NULL,                  -- paid towards this month's EMI
    recovered       REAL NOT NULL,                  -- paid towards opening arrears
    paid_full       INTEGER NOT NULL,               -- 1 = this month's EMI paid in full
    outstanding_end REAL NOT NULL,
    bureau_score    INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, month, loan_id)
);

CREATE TABLE IF NOT EXISTS fact_calls (             -- collection calls (D5 style)
    tenant_id   TEXT NOT NULL,
    call_id     TEXT NOT NULL,
    month       TEXT NOT NULL,
    call_date   TEXT NOT NULL,
    loan_id     TEXT NOT NULL,
    product     TEXT NOT NULL,
    city        TEXT NOT NULL,
    region      TEXT NOT NULL,
    agency      TEXT NOT NULL,
    bucket      TEXT NOT NULL,
    outcome     TEXT NOT NULL,                      -- connected | no_answer | wrong_number
    disposition TEXT NOT NULL,                      -- ptp | callback | refuses | dispute | paid_already | none
    duration_s  INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, call_id)
);

CREATE TABLE IF NOT EXISTS fact_ptp (               -- promises to pay (D6 style)
    tenant_id       TEXT NOT NULL,
    ptp_id          TEXT NOT NULL,
    month           TEXT NOT NULL,
    loan_id         TEXT NOT NULL,
    product         TEXT NOT NULL,
    city            TEXT NOT NULL,
    region          TEXT NOT NULL,
    agency          TEXT NOT NULL,
    bucket          TEXT NOT NULL,
    promised_amount REAL NOT NULL,
    paid_amount     REAL NOT NULL,
    kept            INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, ptp_id)
);

CREATE TABLE IF NOT EXISTS fact_payments (          -- payment attempts (D7 style)
    tenant_id  TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    month      TEXT NOT NULL,
    loan_id    TEXT NOT NULL,
    product    TEXT NOT NULL,
    city       TEXT NOT NULL,
    region     TEXT NOT NULL,
    agency     TEXT NOT NULL,
    channel    TEXT NOT NULL,                       -- nach | upi | app | cash | cheque
    amount     REAL NOT NULL,
    status     TEXT NOT NULL,                       -- success | failed | bounced
    PRIMARY KEY (tenant_id, payment_id)
);

CREATE TABLE IF NOT EXISTS fact_disputes (
    tenant_id       TEXT NOT NULL,
    dispute_id      TEXT NOT NULL,
    month           TEXT NOT NULL,
    loan_id         TEXT NOT NULL,
    product         TEXT NOT NULL,
    city            TEXT NOT NULL,
    region          TEXT NOT NULL,
    agency          TEXT NOT NULL,
    reason          TEXT NOT NULL,                  -- wrong_amount | paid_already | harassment | fraud | other
    resolved        INTEGER NOT NULL,
    days_to_resolve INTEGER,
    PRIMARY KEY (tenant_id, dispute_id)
);

-- ---------- users, history, audit (tenant-scoped like everything else) ----------
CREATE TABLE IF NOT EXISTS lc_users (               -- tenant_id = 'GLOBAL'
    tenant_id     TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL,                    -- lender_user | csm
    lender_scope  TEXT NOT NULL,                    -- JSON list of lender ids
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS qa_log (                 -- every question asked and what the agent did
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,                      -- the user's lender, or 'GLOBAL' for a multi-lender user
    user_id     TEXT NOT NULL,
    asked_at    TEXT NOT NULL,
    question    TEXT NOT NULL,
    status      TEXT NOT NULL,                      -- answered | clarify | refused | no_data | error
    metric_id   TEXT,
    sql_text    TEXT,
    latency_ms  INTEGER,
    response    TEXT                                -- full JSON returned to the user
);
CREATE INDEX IF NOT EXISTS ix_qa_log_user ON qa_log (tenant_id, user_id, id);

CREATE TABLE IF NOT EXISTS audit_log (              -- refusals (cross-tenant, write attempts, raw SQL, injection)
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    logged_at  TEXT NOT NULL,
    verdict    TEXT NOT NULL,                       -- cross_tenant | write_attempt | raw_sql | injection | out_of_scope
    detail     TEXT NOT NULL,
    question   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_user ON audit_log (tenant_id, user_id, id);

-- indexes for the metric queries (every query filters tenant_id + month)
CREATE INDEX IF NOT EXISTS ix_perf_tm   ON fact_perf (tenant_id, month);
CREATE INDEX IF NOT EXISTS ix_calls_tm  ON fact_calls (tenant_id, month);
CREATE INDEX IF NOT EXISTS ix_ptp_tm    ON fact_ptp (tenant_id, month);
CREATE INDEX IF NOT EXISTS ix_pay_tm    ON fact_payments (tenant_id, month);
CREATE INDEX IF NOT EXISTS ix_disp_tm   ON fact_disputes (tenant_id, month);

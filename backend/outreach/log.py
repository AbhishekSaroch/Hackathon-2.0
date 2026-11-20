"""
The send log: one row for every attempt to send anything (sent, mock, blocked, failed). This is the audit trail.

It lives in the main database (portfolioiq.db) next to the recommendations it came from, and goes through db.py like everything else,
so it is tenant-scoped: one lender can never read another lender's log.
Nothing here stores a secret. `recipient_hint` is the MASKED test recipient; `intended_for` says who the message is really for
(a customer's first name or a team), because in this demo every message goes to the test recipient.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "db")]
import db  # noqa: E402

from . import config  # noqa: E402

DB = "main"
STATUSES = ("sent", "mock_sent", "blocked", "failed")

DDL = """
CREATE TABLE IF NOT EXISTS outreach_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    rec_id         INTEGER,                 -- recommendations.id this came from (BI-02 "To approve"), if any
    account_id     TEXT,
    channel        TEXT NOT NULL,           -- email | whatsapp | sms
    purpose        TEXT,                    -- customer_reminder | team_handoff | ...
    intended_for   TEXT,                    -- who the message is really for (first name, team), never an address
    recipient_hint TEXT,                    -- masked test recipient the message actually went to
    mode           TEXT NOT NULL,           -- mock | live
    status         TEXT NOT NULL,           -- sent | mock_sent | blocked | failed
    provider_ref   TEXT,                    -- message id from the provider
    error          TEXT,                    -- scrubbed of secrets
    message        TEXT,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_outreach_rec ON outreach_log(tenant_id, rec_id, purpose);
CREATE TABLE IF NOT EXISTS collection_holds (       -- "pause collection calls": set when a disputed account is handed to the dispute team
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL,
    account_id     TEXT NOT NULL,
    rec_id         INTEGER,
    reason         TEXT,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    released_at    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_hold_account ON collection_holds(tenant_id, account_id);
"""
_ready = False


def ensure_table() -> None:
    global _ready
    if _ready and db.DB_PATH.exists():
        return
    with db.get_conn(DB) as conn:
        conn.executescript(DDL)
    _ready = True


def record(tenant: str, *, channel: str, mode: str, status: str, rec_id: int | None = None, account_id: str | None = None, purpose: str | None = None,
           intended_for: str | None = None, recipient_hint: str | None = None, provider_ref: str | None = None, error: str | None = None,
           message: str | None = None) -> int:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    ensure_table()
    return db.insert(tenant, "outreach_log", {
        "rec_id": rec_id, "account_id": account_id, "channel": channel, "purpose": purpose, "intended_for": intended_for,
        "recipient_hint": recipient_hint, "mode": mode, "status": status, "provider_ref": provider_ref,
        "error": config.scrub(error) if error else None, "message": config.scrub(message) if message else None}, database=DB)


def recent(tenant: str, limit: int = 50) -> list[dict]:
    ensure_table()
    return db.query(tenant, "SELECT id, rec_id, account_id, channel, purpose, intended_for, recipient_hint, mode, status, provider_ref, error, message, created_at "
                            "FROM outreach_log WHERE tenant_id = :tenant_id ORDER BY id DESC LIMIT :n", {"n": int(limit)}, database=DB)


def recent_customer_messages(tenant: str, account_id: str, days: int = 7) -> int:
    """Customer reminders sent (or mock-sent) to this account in the last `days` days: the frequency cap."""
    ensure_table()
    r = db.query_one(tenant, "SELECT COUNT(*) AS n FROM outreach_log WHERE tenant_id = :tenant_id AND account_id = :a AND purpose = 'customer_reminder' "
                             "AND status IN ('sent', 'mock_sent') AND created_at >= datetime('now', :d)", {"a": account_id, "d": f"-{int(days)} days"}, database=DB)
    return r["n"]


def add_hold(tenant: str, account_id: str, rec_id: int | None, reason: str) -> bool:
    """Pause collection calls on an account. True if a new hold was made, False if it already had one."""
    ensure_table()
    with db.get_conn(DB) as conn:
        cur = conn.execute("INSERT OR IGNORE INTO collection_holds (tenant_id, account_id, rec_id, reason) VALUES (?, ?, ?, ?)", (tenant, account_id, rec_id, reason))
        return cur.rowcount > 0


def holds(tenant: str) -> list[dict]:
    ensure_table()
    return db.query(tenant, "SELECT account_id, rec_id, reason, created_at FROM collection_holds WHERE tenant_id = :tenant_id AND released_at IS NULL", database=DB)


def already_done(tenant: str, rec_id: int, purpose: str) -> bool:
    """True if this recommendation already went out for this purpose (sent or mock_sent). Used so nothing is sent twice."""
    ensure_table()
    r = db.query_one(tenant, "SELECT COUNT(*) AS n FROM outreach_log WHERE tenant_id = :tenant_id AND rec_id = :r AND purpose = :p "
                             "AND status IN ('sent', 'mock_sent')", {"r": int(rec_id), "p": purpose}, database=DB)
    return r["n"] > 0

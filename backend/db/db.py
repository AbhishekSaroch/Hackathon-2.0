"""
Tenant-safe SQLite access. Every data function in the project goes through here.

Rules enforced in code (not in prompts):
  - read queries MUST contain the :tenant_id placeholder, otherwise they are refused
  - tenant_id is always bound by this module, callers cannot override it in params
"""
import os
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "schema.sql"
DB_PATH = Path(os.getenv("PORTFOLIOIQ_DB", HERE.parent / "portfolioiq.db"))


class TenantError(Exception):
    """Raised when a query is not tenant-scoped."""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)      # wait instead of "database is locked"
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")         # readers and writers do not block each other
    return conn


def init_db() -> None:
    """Create all tables from schema.sql (safe to run again)."""
    with get_conn() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def _check_scoped(sql: str, params: dict) -> None:
    if ":tenant_id" not in sql:
        raise TenantError("Query refused: it must filter on :tenant_id")
    if "tenant_id" in params:
        raise TenantError("Query refused: do not pass tenant_id in params")


def query(tenant_id: str, sql: str, params: dict | None = None) -> list[dict]:
    """Run a SELECT scoped to one tenant. SQL must use :tenant_id."""
    params = dict(params or {})
    _check_scoped(sql, params)
    params["tenant_id"] = tenant_id
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(tenant_id: str, sql: str, params: dict | None = None) -> dict | None:
    rows = query(tenant_id, sql, params)
    return rows[0] if rows else None


def insert(tenant_id: str, table: str, row: dict) -> int:
    """Insert one row; tenant_id is added automatically. Returns the new rowid."""
    if "tenant_id" in row:
        raise TenantError("Insert refused: do not pass tenant_id in the row")
    data = {**row, "tenant_id": tenant_id}
    cols = ", ".join(data)
    marks = ", ".join(f":{k}" for k in data)
    with get_conn() as conn:
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", data)
        return cur.lastrowid


def insert_many(tenant_id: str, table: str, rows: list[dict]) -> int:
    """Bulk insert (used by the CSV loader)."""
    if not rows:
        return 0
    if any("tenant_id" in r for r in rows):
        raise TenantError("Insert refused: do not pass tenant_id in rows")
    cols_list = list(rows[0].keys()) + ["tenant_id"]
    cols = ", ".join(cols_list)
    marks = ", ".join(f":{c}" for c in cols_list)
    data = [{**r, "tenant_id": tenant_id} for r in rows]
    with get_conn() as conn:
        conn.executemany(f"INSERT INTO {table} ({cols}) VALUES ({marks})", data)
    return len(data)


def execute(tenant_id: str, sql: str, params: dict | None = None) -> int:
    """UPDATE / DELETE scoped to one tenant. SQL must use :tenant_id."""
    params = dict(params or {})
    _check_scoped(sql, params)
    params["tenant_id"] = tenant_id
    with get_conn() as conn:
        return conn.execute(sql, params).rowcount


if __name__ == "__main__":
    init_db()
    print(f"DB ready at {DB_PATH}")

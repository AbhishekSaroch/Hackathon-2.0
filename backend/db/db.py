"""
Tenant-safe SQLite access. Every data function in the project goes through here.

Rules enforced in code (not in prompts):
  - read queries MUST contain the :tenant_id placeholder, otherwise they are refused
  - tenant_id is always bound by this module, callers cannot override it in params

Three databases live side by side:
  - "main" (default): portfolioiq.db, used by BI-02. Behaviour is unchanged from before.
  - "bi03":           bi03.db, used by BI-03 (big event stream, kept apart so BI-02 stays small and fast).
  - "lc04":           lc04.db, used by LC-04 (intraday event stream + 15-minute cube; tenant = lender).
  - "lc01":           lc01.db, used by LC-01 (ask-your-portfolio warehouse; tenant = lender, 3 lenders).
Every function takes an optional database="main" argument at the end, so existing callers are untouched.
"""
import os
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "schema.sql"
DB_PATH = Path(os.getenv("PORTFOLIOIQ_DB", HERE.parent / "portfolioiq.db"))
BI03_SCHEMA_PATH = HERE / "schema_bi03.sql"
BI03_DB_PATH = Path(os.getenv("PORTFOLIOIQ_BI03_DB", HERE.parent / "bi03.db"))
LC04_SCHEMA_PATH = HERE / "schema_lc04.sql"
LC04_DB_PATH = Path(os.getenv("PORTFOLIOIQ_LC04_DB", HERE.parent / "lc04.db"))
LC01_SCHEMA_PATH = HERE / "schema_lc01.sql"
LC01_DB_PATH = Path(os.getenv("PORTFOLIOIQ_LC01_DB", HERE.parent / "lc01.db"))
ASK_SCHEMA_PATH = HERE / "schema_ask.sql"
ASK_DB_PATH = Path(os.getenv("PORTFOLIOIQ_ASK_DB", HERE.parent / "ask_all.db"))   # read-only COPY of BI-02, BI-03 and LC-04 tables (made by data/sync_all.py)


class TenantError(Exception):
    """Raised when a query is not tenant-scoped."""


def _paths(database: str) -> tuple[Path, Path]:
    """(db file, schema file) for a database name. Read at call time so tests can point elsewhere."""
    if database == "main":
        return DB_PATH, SCHEMA_PATH
    if database == "bi03":
        return BI03_DB_PATH, BI03_SCHEMA_PATH
    if database == "lc04":
        return LC04_DB_PATH, LC04_SCHEMA_PATH
    if database == "lc01":
        return LC01_DB_PATH, LC01_SCHEMA_PATH
    if database == "ask":
        return ASK_DB_PATH, ASK_SCHEMA_PATH
    raise ValueError(f"unknown database {database!r} (use 'main', 'bi03', 'lc04', 'lc01' or 'ask')")


def get_conn(database: str = "main") -> sqlite3.Connection:
    path, _ = _paths(database)
    conn = sqlite3.connect(path, timeout=30)          # wait instead of "database is locked"
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")         # readers and writers do not block each other
    if database in ("bi03", "lc04", "lc01", "ask"):
        conn.execute("PRAGMA synchronous = NORMAL")   # faster bulk loads, still safe with WAL
    return conn


def init_db(database: str = "main") -> None:
    """Create all tables from the database's schema file (safe to run again)."""
    _, schema = _paths(database)
    with get_conn(database) as conn:
        conn.executescript(schema.read_text(encoding="utf-8"))


def _check_scoped(sql: str, params: dict) -> None:
    if ":tenant_id" not in sql:
        raise TenantError("Query refused: it must filter on :tenant_id")
    if "tenant_id" in params:
        raise TenantError("Query refused: do not pass tenant_id in params")


def query(tenant_id: str, sql: str, params: dict | None = None, database: str = "main") -> list[dict]:
    """Run a SELECT scoped to one tenant. SQL must use :tenant_id."""
    params = dict(params or {})
    _check_scoped(sql, params)
    params["tenant_id"] = tenant_id
    with get_conn(database) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_df(tenant_id: str, sql: str, params: dict | None = None, database: str = "main"):
    """Same tenant rules as query(), but returns a pandas DataFrame (fast for big tables like BI-03 events)."""
    import pandas as pd  # imported here so BI-02 never needs pandas

    params = dict(params or {})
    _check_scoped(sql, params)
    params["tenant_id"] = tenant_id
    conn = get_conn(database)
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def query_one(tenant_id: str, sql: str, params: dict | None = None, database: str = "main") -> dict | None:
    rows = query(tenant_id, sql, params, database)
    return rows[0] if rows else None


def insert(tenant_id: str, table: str, row: dict, database: str = "main") -> int:
    """Insert one row; tenant_id is added automatically. Returns the new rowid."""
    if "tenant_id" in row:
        raise TenantError("Insert refused: do not pass tenant_id in the row")
    data = {**row, "tenant_id": tenant_id}
    cols = ", ".join(data)
    marks = ", ".join(f":{k}" for k in data)
    with get_conn(database) as conn:
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", data)
        return cur.lastrowid


def insert_many(tenant_id: str, table: str, rows: list[dict], database: str = "main") -> int:
    """Bulk insert (used by the CSV loaders)."""
    if not rows:
        return 0
    if any("tenant_id" in r for r in rows):
        raise TenantError("Insert refused: do not pass tenant_id in rows")
    cols_list = list(rows[0].keys()) + ["tenant_id"]
    cols = ", ".join(cols_list)
    marks = ", ".join(f":{c}" for c in cols_list)
    data = [{**r, "tenant_id": tenant_id} for r in rows]
    with get_conn(database) as conn:
        conn.executemany(f"INSERT INTO {table} ({cols}) VALUES ({marks})", data)
    return len(data)


def execute(tenant_id: str, sql: str, params: dict | None = None, database: str = "main") -> int:
    """UPDATE / DELETE scoped to one tenant. SQL must use :tenant_id."""
    params = dict(params or {})
    _check_scoped(sql, params)
    params["tenant_id"] = tenant_id
    with get_conn(database) as conn:
        return conn.execute(sql, params).rowcount


if __name__ == "__main__":
    import sys

    name = sys.argv[1] if len(sys.argv) > 1 else "main"      # python db.py        -> BI-02 database
    init_db(name)                                             # python db.py bi03   -> BI-03 database
    print(f"DB '{name}' ready at {_paths(name)[0]}")

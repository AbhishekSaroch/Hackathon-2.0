"""
LC-01 users, scope and audit. The lender scope belongs to the USER RECORD in the database, never to a request or a prompt.

    p = login("u_l1")                          # reads lc_users; Principal(lenders=("L1",))
    warehouse_query(p, sql, params, tenants)   # runs a checked SELECT once per lender in scope, through db.py (:tenant_id bound there)

Guarantees (each one is tested in tests/test_lc01_access.py):
  - a forged or edited Principal is useless: every call re-reads the user and compares
  - asking for a lender outside the scope raises AccessError (it is never silently narrowed or silently empty)
  - the SQL checker refuses anything that is not a single read-only SELECT over the whitelisted warehouse tables with :tenant_id
  - db.py refuses a query without :tenant_id and binds the tenant itself: even a buggy caller cannot read another lender's rows
"""
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
import db  # noqa: E402

import lc01_registry as R  # noqa: E402

DB = "lc01"

USERS = [
    dict(user_id="u_l1", display_name="Kaveri Finance · risk manager", role="lender_user", lender_scope=["L1"]),
    dict(user_id="u_l2", display_name="Lakshya Capital · collections manager", role="lender_user", lender_scope=["L2"]),
    dict(user_id="u_l3", display_name="Meridian Credit · risk manager", role="lender_user", lender_scope=["L3"]),
    dict(user_id="u_csm", display_name="CN client success manager (all 3 lenders)", role="csm", lender_scope=["L1", "L2", "L3"]),
    dict(user_id="u_csm_b", display_name="CN client success manager (L2, L3)", role="csm", lender_scope=["L2", "L3"]),
    # users of the other modules' data (read from ask_all.db, see lc01_sources.py)
    dict(user_id="u_abc", display_name="ABC Bank · collections head (BI-02 / BI-03 data)", role="lender_user", lender_scope=["abc_bank"]),
    dict(user_id="u_cn", display_name="CN leadership (3 lenders + command center)", role="cn_leader", lender_scope=["L1", "L2", "L3"]),
]
USERS.insert(0, dict(user_id="u_neha", display_name="Neha Kapoor · Collections Head", role="collections_head", lender_scope=["L1", "L2", "L3"]))
UI_USERS = ("u_neha",)             # the only user the app shows and signs in as; the others stay for the access tests, the evaluation and API callers
EXTRA_TENANTS = ("abc_bank",)     # tenants that exist only in ask_all.db (BI-02 / BI-03); they have no rows in the LC-01 warehouse


class AccessError(Exception):
    """The caller asked for something outside its scope (maps to HTTP 403)."""


class SqlRefused(Exception):
    """A statement failed the SQL checker (never reaches the database)."""


@dataclass(frozen=True)
class Principal:
    user_id: str
    display_name: str
    role: str
    lenders: tuple

    @property
    def multi(self) -> bool:
        return len(self.lenders) > 1

    @property
    def log_tenant(self) -> str:
        """Tenant id under which this user's history and audit rows are stored."""
        return self.lenders[0] if len(self.lenders) == 1 else "GLOBAL"


def sync_users(database: str = DB) -> int:
    db.execute("GLOBAL", "DELETE FROM lc_users WHERE tenant_id=:tenant_id", database=database)
    return db.insert_many("GLOBAL", "lc_users", [dict(user_id=u["user_id"], display_name=u["display_name"], role=u["role"],
                                                      lender_scope=json.dumps(u["lender_scope"])) for u in USERS], database=database)


def login(user_id: str, database: str = DB) -> Principal:
    u = db.query_one("GLOBAL", "SELECT * FROM lc_users WHERE tenant_id=:tenant_id AND user_id=:u", {"u": str(user_id)}, database)
    if not u:
        raise AccessError("unknown user")
    lenders = tuple(sorted(json.loads(u["lender_scope"])))
    if not lenders or not set(lenders) <= set(R.LENDER_IDS) | set(EXTRA_TENANTS):
        raise AccessError("user has no valid lender scope")
    if u["role"] == "lender_user" and len(lenders) != 1:
        raise AccessError("a lender user must have exactly one lender")
    return Principal(u["user_id"], u["display_name"], u["role"], lenders)


def verify(p) -> Principal:
    if not isinstance(p, Principal):
        raise AccessError("not a principal")
    if login(p.user_id) != p:
        raise AccessError("principal does not match the user record")
    return p


def require_lenders(p: Principal, wanted) -> tuple:
    """Lenders a request may touch: must be inside the user's scope, otherwise AccessError (never narrowed silently)."""
    p = verify(p)
    wanted = tuple(wanted) if wanted else p.lenders
    bad = [w for w in wanted if w not in p.lenders]
    if bad:
        raise AccessError(f"lender {', '.join(bad)} is outside your scope")
    return wanted


# ---------------- SQL checker (defence in depth: the compiler is the only writer of SQL, but every statement is still checked) -------------
_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex|truncate|grant|revoke|union|with|or|not|except|intersect)\b", re.I)
_TENANT_EQ = re.compile(r"\btenant_id\s*=\s*:tenant_id\b", re.I)
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_\.]*)", re.I)


def check_sql(sql: str, allowed=None) -> str:
    """Raise SqlRefused unless `sql` is ONE read-only SELECT over whitelisted tables and filters on :tenant_id.
    `allowed` (a set of table names) is for the ask_all.db sources; by default only the LC-01 warehouse tables are allowed."""
    allowed = R.TABLES_ALLOWED if allowed is None else allowed
    s = sql.strip()
    if not s:
        raise SqlRefused("empty statement")
    if ";" in s.rstrip(";"):
        raise SqlRefused("more than one statement")
    s = s.rstrip(";").strip()
    if "--" in s or "/*" in s:
        raise SqlRefused("comments are not allowed")
    if not re.match(r"^select\b", s, re.I):
        raise SqlRefused("only SELECT statements are allowed")
    if _FORBIDDEN.search(s):
        raise SqlRefused("statement uses a forbidden keyword")
    if ":tenant_id" not in s:
        raise SqlRefused("statement must filter on :tenant_id")
    if not _TENANT_EQ.search(s):
        raise SqlRefused("statement must contain the condition tenant_id = :tenant_id")
    if re.search(r"tenant_id", _TENANT_EQ.sub(" ", s), re.I):
        raise SqlRefused("tenant_id may only appear as tenant_id = :tenant_id (no other comparison, no OR / NOT)")
    tables = _TABLE_REF.findall(s)
    if not tables:
        raise SqlRefused("no table in statement")
    for t in tables:
        if t.lower() not in allowed:
            raise SqlRefused(f"table {t!r} is not part of the governed warehouse")
    return s


def warehouse_query(p: Principal, sql: str, params: dict | None, tenants=None) -> list[dict]:
    """Run a checked SELECT once per lender in scope; every row is tagged with the lender it came from ('_lender')."""
    p = verify(p)
    sql = check_sql(sql)
    rows = []
    for t in require_lenders(p, tenants):
        for r in db.query(t, sql, params, DB):
            r["_lender"] = t
            rows.append(r)
    return rows


# ---------------- audit and history ----------------
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def audit(p: Principal, verdict: str, detail: str, question: str) -> None:
    db.insert(p.log_tenant, "audit_log", dict(user_id=p.user_id, logged_at=_now(), verdict=verdict, detail=detail[:500], question=question[:1000]), DB)


def audit_rows(p: Principal, limit: int = 50) -> list[dict]:
    p = verify(p)
    return db.query(p.log_tenant, "SELECT id, logged_at, verdict, detail, question FROM audit_log WHERE tenant_id=:tenant_id AND user_id=:u ORDER BY id DESC LIMIT :n",
                    {"u": p.user_id, "n": limit}, DB)


def log_answer(p: Principal, question: str, status: str, metric_id, sql_text, latency_ms: int, response: dict) -> int:
    return db.insert(p.log_tenant, "qa_log", dict(user_id=p.user_id, asked_at=_now(), question=question[:1000], status=status, metric_id=metric_id,
                                                  sql_text=sql_text, latency_ms=latency_ms, response=json.dumps(response, default=str)), DB)


def history(p: Principal, limit: int = 30) -> list[dict]:
    p = verify(p)
    rows = db.query(p.log_tenant, "SELECT id, asked_at, question, status, metric_id, latency_ms FROM qa_log WHERE tenant_id=:tenant_id AND user_id=:u ORDER BY id DESC LIMIT :n",
                    {"u": p.user_id, "n": limit}, DB)
    return rows


def principal_json(p: Principal) -> dict:
    return dict(user_id=p.user_id, name=p.display_name, role=p.role, lenders=list(p.lenders),
                lender_names={l: R.LENDER_NAMES.get(l, "ABC Bank" if l == "abc_bank" else l) for l in p.lenders}, multi=p.multi)


if __name__ == "__main__":
    for uid in ("u_l1", "u_csm"):
        print(login(uid))
